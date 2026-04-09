"""Event download pipeline for Luma.

This module owns all network I/O: fetching, parsing, and deduplication.
Its single public function is ``download_events``.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from luma.config import (
    API_BASE,
    PAGINATION_LIMIT,
    REQUEST_DELAY_SEC,
)
from luma.models import Category, Event, EventDetail, Host


def _parse_iso8601_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _extract_slug(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    if not path:
        raise ValueError(f"Could not parse slug from URL: {url}")
    return path


def _request_with_retry(
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
    raise RuntimeError("_request_with_retry failed without explicit error")


def _get_json(url: str, *, web_url: str, retries: int = 5) -> dict[str, Any]:
    payload = _request_with_retry(
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


def _resolve_source_for_calendar_url(
    calendar_slug: str, retries: int = 5
) -> tuple[str, str | None]:
    calendar_url = f"https://luma.com/{calendar_slug}"
    html = _request_with_retry(
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


def _event_from_entry(entry: dict[str, Any], source: str) -> Event | None:
    event = entry.get("event", {})
    api_id = event.get("api_id")
    title = (event.get("name") or "").strip()
    slug = event.get("url")
    start_at = event.get("start_at")
    if not api_id or not title or not slug or not start_at:
        return None

    guest_count = int(entry.get("guest_count") or 0)

    coordinate = event.get("coordinate") or {}
    geo = event.get("geo_address_info") or {}

    raw_hosts = entry.get("hosts") or []
    hosts = [
        Host(
            name=h["name"].strip(),
            linkedin_handle=h.get("linkedin_handle"),
            youtube_handle=h.get("youtube_handle"),
        )
        for h in raw_hosts
        if (h.get("name") or "").strip()
    ]

    return Event(
        id=api_id,
        title=title,
        url=f"https://luma.com/{slug}",
        start_at=start_at,
        guest_count=guest_count,
        sources=[source],
        location_type=event.get("location_type"),
        latitude=coordinate.get("latitude"),
        longitude=coordinate.get("longitude"),
        city=geo.get("city"),
        region=geo.get("region"),
        country=geo.get("country"),
        hosts=hosts,
    )


def _fetch_category_events(
    category_slug: str,
    *,
    latitude: str,
    longitude: str,
    start_utc: datetime,
    end_utc: datetime,
    retries: int,
) -> list[Event]:
    results: list[Event] = []
    web_url = f"https://luma.com/{category_slug}"
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "pagination_limit": PAGINATION_LIMIT,
            "slug": category_slug,
        }
        if cursor:
            params["pagination_cursor"] = cursor
        url = f"{API_BASE}/discover/get-paginated-events?{urllib.parse.urlencode(params)}"
        data = _get_json(url, web_url=web_url, retries=retries)

        entries = data.get("entries", [])
        if not entries:
            break

        for entry in entries:
            ev = _event_from_entry(entry, source=f"category:{category_slug}")
            if not ev:
                continue
            dt = _parse_iso8601_utc(ev.start_at)
            if start_utc <= dt < end_utc:
                results.append(ev)

        if not data.get("has_more"):
            break

        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return results


def _fetch_calendar_events(
    calendar_slug: str,
    *,
    calendar_api_id: str,
    start_utc: datetime,
    end_utc: datetime,
    retries: int,
) -> list[Event]:
    results: list[Event] = []
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
        data = _get_json(url, web_url=web_url, retries=retries)

        entries = data.get("entries", [])
        if not entries:
            break

        for entry in entries:
            ev = _event_from_entry(entry, source=f"calendar:{calendar_slug}")
            if not ev:
                continue
            dt = _parse_iso8601_utc(ev.start_at)
            if start_utc <= dt < end_utc:
                results.append(ev)

        if not data.get("has_more"):
            break

        next_cursor = data.get("next_cursor")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return results


def _dedupe_by_url(events: list[Event]) -> list[Event]:
    merged: dict[str, dict[str, Any]] = {}
    for ev in events:
        if ev.url not in merged:
            merged[ev.url] = {
                "id": ev.id,
                "title": ev.title,
                "url": ev.url,
                "start_at": ev.start_at,
                "guest_count": ev.guest_count,
                "sources": set(ev.sources),
                "location_type": ev.location_type,
                "latitude": ev.latitude,
                "longitude": ev.longitude,
                "city": ev.city,
                "region": ev.region,
                "country": ev.country,
                "hosts": ev.hosts,
            }
            continue

        existing = merged[ev.url]
        existing["guest_count"] = max(existing["guest_count"], ev.guest_count)
        existing["sources"].update(ev.sources)
        if _parse_iso8601_utc(ev.start_at) < _parse_iso8601_utc(existing["start_at"]):
            existing["start_at"] = ev.start_at
            existing["title"] = ev.title

    result: list[Event] = []
    for item in merged.values():
        item["sources"] = sorted(item["sources"])
        result.append(Event.model_validate(item))

    return result


def download_events(
    *,
    retries: int,
    start_utc: datetime,
    end_utc: datetime,
    category_urls: list[str],
    calendars: list[dict[str, str | None]],
    latitude: str,
    longitude: str,
) -> list[Event]:
    """Fetch events from all configured sources and return deduplicated list."""
    all_events: list[Event] = []

    category_slugs = [_extract_slug(url) for url in category_urls]
    for slug in category_slugs:
        print(f"Fetching category events: {slug}", file=sys.stderr)
        all_events.extend(
            _fetch_category_events(slug, latitude=latitude, longitude=longitude, start_utc=start_utc, end_utc=end_utc, retries=retries)
        )

    for cal in calendars:
        slug = _extract_slug(cal["url"])
        calendar_api_id = cal.get("calendar_api_id")
        if calendar_api_id:
            print(f"Fetching calendar events: {slug}", file=sys.stderr)
            all_events.extend(
                _fetch_calendar_events(
                    slug,
                    calendar_api_id=calendar_api_id,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    retries=retries,
                )
            )
            continue

        print(f"Resolving source type for: {slug}", file=sys.stderr)
        source_type, resolved_calendar_id = _resolve_source_for_calendar_url(
            slug, retries=retries
        )
        if source_type == "calendar" and resolved_calendar_id:
            print(f"Fetching calendar events: {slug}", file=sys.stderr)
            all_events.extend(
                _fetch_calendar_events(
                    slug,
                    calendar_api_id=resolved_calendar_id,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    retries=retries,
                )
            )
        else:
            print(f"Fetching discover events via slug fallback: {slug}", file=sys.stderr)
            all_events.extend(
                _fetch_category_events(slug, latitude=latitude, longitude=longitude, start_utc=start_utc, end_utc=end_utc, retries=retries)
            )

    return _dedupe_by_url(all_events)


# ---------------------------------------------------------------------------
# ProseMirror → Markdown
# ---------------------------------------------------------------------------

def _pm_collect_text(node: dict[str, Any]) -> str:
    """Recursively extract plain text from a ProseMirror node."""
    node_type = node.get("type", "")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hard_break":
        return "\n"
    parts: list[str] = []
    for child in node.get("content", []):
        parts.append(_pm_collect_text(child))
    return "".join(parts)


def _prosemirror_to_markdown(doc: dict[str, Any] | None) -> str:
    if not doc or not doc.get("content"):
        return ""

    lines: list[str] = []

    for node in doc["content"]:
        node_type = node.get("type", "")

        if node_type == "paragraph":
            lines.append(_pm_collect_text(node))
            lines.append("")

        elif node_type == "heading":
            level = node.get("attrs", {}).get("level", 1)
            prefix = "#" * level
            lines.append(f"{prefix} {_pm_collect_text(node)}")
            lines.append("")

        elif node_type == "bullet_list":
            for item in node.get("content", []):
                lines.append(f"- {_pm_collect_text(item)}")
            lines.append("")

        elif node_type == "ordered_list":
            for idx, item in enumerate(node.get("content", []), start=1):
                lines.append(f"{idx}. {_pm_collect_text(item)}")
            lines.append("")

        elif node_type == "horizontal_rule":
            lines.append("---")
            lines.append("")

        elif node_type == "hard_break":
            lines.append("")

    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Single-event detail fetch
# ---------------------------------------------------------------------------

def fetch_event_detail(event_id: str, *, retries: int = 5) -> EventDetail:
    """Fetch full details for a single event by its API ID."""
    url = f"{API_BASE}/event/get?{urllib.parse.urlencode({'event_api_id': event_id})}"
    data = _get_json(url, web_url="https://luma.com", retries=retries)

    description_md = _prosemirror_to_markdown(data.get("description_mirror"))

    categories = [
        Category(
            api_id=cat["api_id"],
            name=cat.get("name", ""),
            slug=cat.get("slug", ""),
        )
        for cat in data.get("categories", [])
        if cat.get("api_id")
    ]

    return EventDetail(
        event_id=event_id,
        description_md=description_md,
        categories=categories,
    )
