"""Location enrichment for downloaded events.

Pass 1: Nominatim geocoding for events with partial location data.
Pass 2+3: LLM inference for zero-data events and gap-filling after Nominatim.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.parse

import anthropic

from luma.config import (
    AGENT_MAX_TOKENS,
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_AGENT_MODEL,
    LLM_ENRICH_BATCH_SIZE,
    NOMINATIM_BASE_URL,
    NOMINATIM_DELAY_SEC,
    NOMINATIM_USER_AGENT,
)
from luma.download import _request_with_retry
from luma.models import Event

from dataclasses import replace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_coords(event: Event) -> bool:
    return event.latitude is not None and event.longitude is not None


def _missing_fields(event: Event) -> list[str]:
    missing: list[str] = []
    if event.latitude is None or event.longitude is None:
        if "latitude" not in missing:
            missing.append("latitude")
        if "longitude" not in missing:
            missing.append("longitude")
    if event.city is None:
        missing.append("city")
    if event.region is None:
        missing.append("region")
    if event.country is None:
        missing.append("country")
    return missing


def _is_candidate(event: Event) -> bool:
    if event.location_type and event.location_type.lower() == "online":
        return False
    return len(_missing_fields(event)) > 0


# ---------------------------------------------------------------------------
# Nominatim
# ---------------------------------------------------------------------------

def _nominatim_get(url: str) -> dict:
    data = _request_with_retry(
        url,
        headers={"User-Agent": NOMINATIM_USER_AGENT},
    )
    time.sleep(NOMINATIM_DELAY_SEC)
    return json.loads(data.decode("utf-8"))


def _parse_nominatim_city(address: dict) -> str | None:
    return address.get("city") or address.get("town") or address.get("village")


def _nominatim_reverse(lat: float, lon: float) -> dict:
    try:
        params = urllib.parse.urlencode({"lat": lat, "lon": lon, "format": "json"})
        url = f"{NOMINATIM_BASE_URL}/reverse?{params}"
        resp = _nominatim_get(url)
        address = resp.get("address", {})
        result: dict = {}
        city = _parse_nominatim_city(address)
        if city:
            result["city"] = city
        state = address.get("state")
        if state:
            result["region"] = state
        country = address.get("country")
        if country:
            result["country"] = country
        return result
    except Exception:
        return {}


def _nominatim_forward(
    city: str | None, region: str | None, country: str | None
) -> dict:
    try:
        parts = [p for p in (city, region, country) if p]
        if not parts:
            return {}
        query = ", ".join(parts)
        params = urllib.parse.urlencode({"q": query, "format": "json", "limit": "1"})
        url = f"{NOMINATIM_BASE_URL}/search?{params}"
        resp = _nominatim_get(url)
        if not resp or not isinstance(resp, list) or len(resp) == 0:
            return {}
        first = resp[0]
        lat = float(first["lat"])
        lon = float(first["lon"])
        result: dict = {"latitude": lat, "longitude": lon}
        reverse = _nominatim_reverse(lat, lon)
        result.update(reverse)
        return result
    except Exception:
        return {}


_COORD_PRECISION = 2  # ~1.1 km — sufficient for city-level dedup


def _build_nominatim_key(event: Event) -> str | None:
    if _has_coords(event):
        lat = round(event.latitude, _COORD_PRECISION)  # type: ignore[arg-type]
        lon = round(event.longitude, _COORD_PRECISION)  # type: ignore[arg-type]
        return f"rev:{lat},{lon}"
    if event.city or event.region or event.country:
        return (
            f"fwd:{(event.city or '').lower()}"
            f"|{(event.region or '').lower()}"
            f"|{(event.country or '').lower()}"
        )
    return None


def _apply_result(event: Event, result: dict) -> Event:
    changes: dict = {}
    for fld in ("latitude", "longitude", "city", "region", "country"):
        if getattr(event, fld) is None and fld in result and result[fld] is not None:
            changes[fld] = result[fld]
    if not changes:
        return event
    return replace(event, **changes)


_GEOCODE_CACHE_PATH = pathlib.Path.home() / ".luma" / "coordinates-to-city.json"


def _load_disk_cache() -> dict[str, dict]:
    try:
        with open(_GEOCODE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_disk_cache(cache: dict[str, dict]) -> None:
    try:
        _GEOCODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except OSError as exc:
        print(f"Warning: could not save geocode cache: {exc}", file=sys.stderr)


def _enrich_nominatim(events: list[Event]) -> tuple[list[Event], int]:
    disk_cache = _load_disk_cache()
    cache: dict[str, dict] = {}
    key_map: dict[int, str] = {}
    key_source: dict[str, Event] = {}

    for i, ev in enumerate(events):
        if not _is_candidate(ev):
            continue
        key = _build_nominatim_key(ev)
        if key is None:
            continue
        key_map[i] = key
        if key not in cache:
            if key in disk_cache:
                cache[key] = disk_cache[key]
            else:
                cache[key] = None  # type: ignore[assignment]
                key_source[key] = ev

    to_fetch = [k for k, v in cache.items() if v is None]
    from_cache = len(cache) - len(to_fetch)
    if to_fetch or from_cache:
        parts = []
        if to_fetch:
            parts.append(f"{len(to_fetch)} to fetch")
        if from_cache:
            parts.append(f"{from_cache} from cache")
        print(
            f"Looking up {len(cache)} unique locations ({', '.join(parts)})",
            file=sys.stderr,
        )

    dirty = False
    for ki, key in enumerate(to_fetch, 1):
        ev = key_source[key]
        try:
            if key.startswith("rev:"):
                print(
                    f"Geocoding {ki}/{len(to_fetch)}: resolving city by coords {ev.latitude}, {ev.longitude}",
                    file=sys.stderr,
                )
                cache[key] = _nominatim_reverse(ev.latitude, ev.longitude)  # type: ignore[arg-type]
            elif key.startswith("fwd:"):
                label = ", ".join(p for p in (ev.city, ev.region, ev.country) if p)
                print(
                    f"Geocoding {ki}/{len(to_fetch)}: resolving coords by location {label}",
                    file=sys.stderr,
                )
                cache[key] = _nominatim_forward(ev.city, ev.region, ev.country)
            else:
                cache[key] = {}
        except Exception as exc:
            print(f"Warning: Nominatim lookup failed: {exc}", file=sys.stderr)
            cache[key] = {}
        if cache[key]:
            disk_cache[key] = cache[key]
            dirty = True

    if dirty:
        _save_disk_cache(disk_cache)

    enriched_count = 0
    result_events = list(events)
    for i, key in key_map.items():
        nominatim_result = cache.get(key, {})
        if not nominatim_result:
            continue
        old = result_events[i]
        new = _apply_result(old, nominatim_result)
        if new is not old:
            result_events[i] = new
            enriched_count += 1

    return result_events, enriched_count


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def _build_llm_prompt(events_with_info: list[tuple[int, Event, list[str]]]) -> str:
    lines = [
        "You are given a list of events with missing location fields.",
        "For each event, fill in the missing fields based on the event title, sources, and any existing location fields.",
        "",
        "Rules:",
        '- Use full proper names, not abbreviations. Examples: "San Francisco" not "SF", "California" not "CA", "United States" not "US".',
        "- Return null for any field you cannot confidently determine.",
        "- Only fill fields listed as missing for each event.",
        "",
        "Source hints (calendar/category names that imply a city):",
        '- "sf", "genai-sf", "frontiertower", "sf-hardware-meetup", "sfaiengineers" → San Francisco, California, United States',
        "",
        "Respond with a JSON array. Each element has an \"index\" matching the event index and values for the missing fields.",
        'Example response: [{"index": 0, "city": "San Francisco", "region": "California", "country": "United States", "latitude": null, "longitude": null}]',
        "",
        "Events:",
    ]

    for idx, ev, missing in events_with_info:
        existing = []
        for fld in ("latitude", "longitude", "city", "region", "country"):
            val = getattr(ev, fld)
            if val is not None:
                existing.append(f"{fld}={val}")
        lines.append(f"- Index {idx}: title={ev.title!r}, sources={ev.sources!r}")
        if existing:
            lines.append(f"  Existing: {', '.join(existing)}")
        lines.append(f"  Missing: {', '.join(missing)}")

    return "\n".join(lines)


def _parse_llm_response(text: str) -> list[dict]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return []


def _has_any_location(event: Event) -> bool:
    return (
        _has_coords(event)
        or event.city is not None
        or event.region is not None
        or event.country is not None
    )


def _llm_call(
    client: anthropic.Anthropic,
    candidates: list[tuple[int, Event, list[str]]],
    description: str,
    result_events: list[Event],
) -> int:
    if not candidates:
        return 0

    enriched = 0
    total_batches = (len(candidates) + LLM_ENRICH_BATCH_SIZE - 1) // LLM_ENRICH_BATCH_SIZE
    for batch_num, batch_start in enumerate(
        range(0, len(candidates), LLM_ENRICH_BATCH_SIZE), 1
    ):
        batch = candidates[batch_start : batch_start + LLM_ENRICH_BATCH_SIZE]
        print(
            f"{description}: batch {batch_num}/{total_batches} ({len(batch)} events)",
            file=sys.stderr,
        )
        prompt = _build_llm_prompt(batch)
        try:
            response = client.messages.create(
                model=DEFAULT_AGENT_MODEL,
                max_tokens=AGENT_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            parsed = _parse_llm_response(text)
            idx_to_batch = {entry[0]: entry for entry in batch}
            for item in parsed:
                event_idx = item.get("index")
                if event_idx is None or event_idx not in idx_to_batch:
                    continue
                fill: dict = {}
                _, ev, missing = idx_to_batch[event_idx]
                for fld in missing:
                    val = item.get(fld)
                    if val is not None:
                        fill[fld] = val
                if fill:
                    old = result_events[event_idx]
                    new = _apply_result(old, fill)
                    if new is not old:
                        result_events[event_idx] = new
                        enriched += 1
        except Exception as exc:
            print(f"Warning: LLM enrichment failed: {exc}", file=sys.stderr)

    return enriched


def _enrich_llm(events: list[Event]) -> tuple[list[Event], int]:
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not api_key:
        return events, 0

    gap_fill: list[tuple[int, Event, list[str]]] = []
    zero_data: list[tuple[int, Event, list[str]]] = []
    for i, ev in enumerate(events):
        if not _is_candidate(ev):
            continue
        missing = _missing_fields(ev)
        if not missing:
            continue
        if _has_any_location(ev):
            gap_fill.append((i, ev, missing))
        else:
            zero_data.append((i, ev, missing))

    if not gap_fill and not zero_data:
        return events, 0

    result_events = list(events)
    client = anthropic.Anthropic(api_key=api_key)

    filled = _llm_call(
        client, gap_fill,
        "Filling gaps left after geocoding via LLM",
        result_events,
    )
    inferred = _llm_call(
        client, zero_data,
        "Inferring location from title and sources via LLM",
        result_events,
    )

    return result_events, filled + inferred


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_events(events: list[Event]) -> list[Event]:
    """Enrich events with missing location data via Nominatim and LLM."""
    candidates = [ev for ev in events if _is_candidate(ev)]
    if not candidates:
        return events

    needed = len(candidates)
    print(f"Enriching {needed} events with missing location data", file=sys.stderr)
    events, geocoded = _enrich_nominatim(events)
    events, inferred = _enrich_llm(events)

    unresolved = needed - geocoded - inferred
    print(
        f"Location enrichment: {needed} needed, {geocoded} geocoded, "
        f"{inferred} inferred, {unresolved} unresolved",
        file=sys.stderr,
    )
    return events
