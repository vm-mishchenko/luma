"""Refresh/fetch pipeline for Luma events.

This module contains network fetching, event parsing/deduplication, and cache writes.
Its public API is `refresh(...)`.
"""

from __future__ import annotations

import json
import pathlib
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

API_BASE = "https://api2.luma.com"
FETCH_WINDOW_DAYS = 14
REQUEST_DELAY_SEC = 0.3
HARDCODED_CATEGORY_URLS = [
    "https://luma.com/ai",
    "https://luma.com/tech",
    "https://luma.com/sf",
]
HARDCODED_CALENDARS = [
    {"url": "https://luma.com/genai-sf", "calendar_api_id": "cal-JTdFQadEz0AOxyV"},
    {"url": "https://luma.com/frontiertower", "calendar_api_id": "cal-Sl7q1nHTRXQzjP2"},
    {"url": "https://luma.com/sf-hardware-meetup", "calendar_api_id": "cal-tFAzNGOZ9xn6kT2"},
    {"url": "https://luma.com/deepmind", "calendar_api_id": "cal-7Q5A70Bz5Idxopu"},
    {"url": "https://luma.com/genai-collective", "calendar_api_id": "cal-E74MDlDKBaeAwXK"},
    {"url": "https://luma.com/sfaiengineers", "calendar_api_id": "cal-EmYs2kgt1D9Gb27"},
]
HARDCODED_LAT = "37.33939"
HARDCODED_LON = "-121.89496"
PAGINATION_LIMIT = "50"


@dataclass
class EventRecord:
    title: str
    url: str
    start_at: str
    guest_count: int
    source: str


def parse_iso8601_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def extract_slug(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    if not path:
        raise ValueError(f"Could not parse slug from URL: {url}")
    return path


def request_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    timeout_sec: int = 30,
    retries: int = 5,
    backoff_base_sec: float = 0.5,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            last_error = err
            if err.code in (429, 500, 502, 503, 504):
                if attempt < retries:
                    retry_after = err.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = float(retry_after)
                    else:
                        delay = backoff_base_sec * (2**attempt) + random.uniform(0.0, 0.3)
                    time.sleep(delay)
                    continue
            raise
        except urllib.error.URLError as err:
            last_error = err
            if attempt < retries:
                delay = backoff_base_sec * (2**attempt) + random.uniform(0.0, 0.3)
                time.sleep(delay)
                continue
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("request_with_retry failed without explicit error")


def get_json(url: str, *, web_url: str, retries: int = 5) -> dict[str, Any]:
    payload = request_with_retry(
        url,
        headers={
            "accept": "*/*",
            "origin": "https://luma.com",
            "referer": "https://luma.com/",
            "user-agent": "Mozilla/5.0",
            "x-luma-client-type": "luma-web",
            "x-luma-web-url": web_url,
        },
        retries=retries,
    )
    time.sleep(REQUEST_DELAY_SEC)
    return json.loads(payload.decode("utf-8"))


def resolve_source_for_calendar_url(
    calendar_slug: str, retries: int = 5
) -> tuple[str, str | None]:
    calendar_url = f"https://luma.com/{calendar_slug}"
    html = request_with_retry(
        calendar_url,
        headers={
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "user-agent": "Mozilla/5.0",
        },
        retries=retries,
    ).decode("utf-8", errors="ignore")

    next_data_match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if next_data_match:
        next_data = json.loads(next_data_match.group(1))
        page_data = (
            next_data.get("props", {})
            .get("pageProps", {})
            .get("initialData", {})
            .get("data", {})
        )
        calendar_obj = page_data.get("calendar")
        if isinstance(calendar_obj, dict):
            api_id = calendar_obj.get("api_id")
            if api_id and str(api_id).startswith("cal-"):
                return ("calendar", str(api_id))
        return ("discover", None)

    match = re.search(r'"calendar_api_id"\s*:\s*"(cal-[^"]+)"', html)
    if match:
        return ("calendar", match.group(1))
    return ("discover", None)


def event_from_entry(entry: dict[str, Any], source: str) -> EventRecord | None:
    event = entry.get("event", {})
    title = (event.get("name") or "").strip()
    slug = event.get("url")
    start_at = event.get("start_at")
    if not title or not slug or not start_at:
        return None
    guest_count = int(entry.get("guest_count") or 0)
    return EventRecord(
        title=title,
        url=f"https://luma.com/{slug}",
        start_at=start_at,
        guest_count=guest_count,
        source=source,
    )


def fetch_category_events(
    category_slug: str,
    *,
    start_utc: datetime,
    end_utc: datetime,
    retries: int,
) -> list[EventRecord]:
    results: list[EventRecord] = []
    web_url = f"https://luma.com/{category_slug}"
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        params = {
            "latitude": HARDCODED_LAT,
            "longitude": HARDCODED_LON,
            "pagination_limit": PAGINATION_LIMIT,
            "slug": category_slug,
        }
        if cursor:
            params["pagination_cursor"] = cursor
        url = f"{API_BASE}/discover/get-paginated-events?{urllib.parse.urlencode(params)}"
        data = get_json(url, web_url=web_url, retries=retries)

        entries = data.get("entries", [])
        if not entries:
            break

        for entry in entries:
            record = event_from_entry(entry, source=f"category:{category_slug}")
            if not record:
                continue
            dt = parse_iso8601_utc(record.start_at)
            if start_utc <= dt < end_utc:
                results.append(record)

        if not data.get("has_more"):
            break

        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return results


def fetch_calendar_events(
    calendar_slug: str,
    *,
    calendar_api_id: str,
    start_utc: datetime,
    end_utc: datetime,
    retries: int,
) -> list[EventRecord]:
    results: list[EventRecord] = []
    web_url = f"https://luma.com/{calendar_slug}"
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        params = {
            "calendar_api_id": calendar_api_id,
            "pagination_limit": PAGINATION_LIMIT,
            "period": "future",
        }
        if cursor:
            params["pagination_cursor"] = cursor
        url = f"{API_BASE}/calendar/get-items?{urllib.parse.urlencode(params)}"
        data = get_json(url, web_url=web_url, retries=retries)

        entries = data.get("entries", [])
        if not entries:
            break

        for entry in entries:
            record = event_from_entry(entry, source=f"calendar:{calendar_slug}")
            if not record:
                continue
            dt = parse_iso8601_utc(record.start_at)
            if start_utc <= dt < end_utc:
                results.append(record)

        if not data.get("has_more"):
            break

        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return results


def dedupe_by_url(records: list[EventRecord]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.url not in merged:
            merged[rec.url] = {
                "title": rec.title,
                "url": rec.url,
                "start_at": rec.start_at,
                "guest_count": rec.guest_count,
                "sources": {rec.source},
            }
            continue

        existing = merged[rec.url]
        existing["guest_count"] = max(existing["guest_count"], rec.guest_count)
        existing["sources"].add(rec.source)
        if parse_iso8601_utc(rec.start_at) < parse_iso8601_utc(existing["start_at"]):
            existing["start_at"] = rec.start_at
            existing["title"] = rec.title

    out = []
    for item in merged.values():
        item["sources"] = sorted(item["sources"])
        out.append(item)
    return out


def _cache_filename(fetched_at: datetime) -> str:
    stamp = fetched_at.strftime("%Y-%m-%d_%H-%M-%S")
    return f"events-{stamp}.json"


def save_cache(
    events: list[dict[str, Any]], fetched_at: datetime, cache_dir: pathlib.Path
) -> pathlib.Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": fetched_at.isoformat(),
        "events": events,
    }
    path = cache_dir / _cache_filename(fetched_at)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def fetch_all_events(*, retries: int) -> list[dict[str, Any]]:
    now_utc = datetime.now(timezone.utc)
    today_la = now_utc.astimezone(ZoneInfo("America/Los_Angeles")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = today_la.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=FETCH_WINDOW_DAYS)

    all_records: list[EventRecord] = []

    category_slugs = [extract_slug(url) for url in HARDCODED_CATEGORY_URLS]
    for slug in category_slugs:
        print(f"Fetching category events: {slug}", file=sys.stderr)
        all_records.extend(
            fetch_category_events(slug, start_utc=start_utc, end_utc=end_utc, retries=retries)
        )

    for cal in HARDCODED_CALENDARS:
        slug = extract_slug(cal["url"])
        calendar_api_id = cal.get("calendar_api_id")
        if calendar_api_id:
            print(f"Fetching calendar events: {slug} ({calendar_api_id})", file=sys.stderr)
            all_records.extend(
                fetch_calendar_events(
                    slug,
                    calendar_api_id=calendar_api_id,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    retries=retries,
                )
            )
            continue

        print(f"Resolving source type for: {slug}", file=sys.stderr)
        source_type, resolved_calendar_id = resolve_source_for_calendar_url(
            slug, retries=retries
        )
        if source_type == "calendar" and resolved_calendar_id:
            print(
                f"Fetching calendar events: {slug} ({resolved_calendar_id})",
                file=sys.stderr,
            )
            all_records.extend(
                fetch_calendar_events(
                    slug,
                    calendar_api_id=resolved_calendar_id,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    retries=retries,
                )
            )
        else:
            print(f"Fetching discover events via slug fallback: {slug}", file=sys.stderr)
            all_records.extend(
                fetch_category_events(slug, start_utc=start_utc, end_utc=end_utc, retries=retries)
            )

    return dedupe_by_url(all_records)


def refresh(*, retries: int, cache_dir: pathlib.Path) -> tuple[int, pathlib.Path]:
    """Fetch all events and write to cache. Returns (event_count, cache_path)."""
    events = fetch_all_events(retries=retries)
    fetched_at = datetime.now(timezone.utc)
    path = save_cache(events, fetched_at, cache_dir)
    return len(events), path
