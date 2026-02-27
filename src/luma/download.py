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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from luma.config import (
    API_BASE,
    HARDCODED_CALENDARS,
    HARDCODED_CATEGORY_URLS,
    HARDCODED_LAT,
    HARDCODED_LON,
    PAGINATION_LIMIT,
    REQUEST_DELAY_SEC,
)
from luma.models import Event, generate_event_id


@dataclass
class _EventRecord:
    title: str
    url: str
    start_at: str
    guest_count: int
    source: str


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


def _event_from_entry(entry: dict[str, Any], source: str) -> _EventRecord | None:
    event = entry.get("event", {})
    title = (event.get("name") or "").strip()
    slug = event.get("url")
    start_at = event.get("start_at")
    if not title or not slug or not start_at:
        return None
    guest_count = int(entry.get("guest_count") or 0)
    return _EventRecord(
        title=title,
        url=f"https://luma.com/{slug}",
        start_at=start_at,
        guest_count=guest_count,
        source=source,
    )


def _fetch_category_events(
    category_slug: str,
    *,
    start_utc: datetime,
    end_utc: datetime,
    retries: int,
) -> list[_EventRecord]:
    results: list[_EventRecord] = []
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
        data = _get_json(url, web_url=web_url, retries=retries)

        entries = data.get("entries", [])
        if not entries:
            break

        for entry in entries:
            record = _event_from_entry(entry, source=f"category:{category_slug}")
            if not record:
                continue
            dt = _parse_iso8601_utc(record.start_at)
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


def _fetch_calendar_events(
    calendar_slug: str,
    *,
    calendar_api_id: str,
    start_utc: datetime,
    end_utc: datetime,
    retries: int,
) -> list[_EventRecord]:
    results: list[_EventRecord] = []
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
            record = _event_from_entry(entry, source=f"calendar:{calendar_slug}")
            if not record:
                continue
            dt = _parse_iso8601_utc(record.start_at)
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


def _dedupe_by_url(records: list[_EventRecord]) -> list[Event]:
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
        if _parse_iso8601_utc(rec.start_at) < _parse_iso8601_utc(existing["start_at"]):
            existing["start_at"] = rec.start_at
            existing["title"] = rec.title

    events: list[Event] = []
    for item in merged.values():
        events.append(Event(
            id=generate_event_id(item["url"]),
            title=item["title"],
            url=item["url"],
            start_at=item["start_at"],
            guest_count=item["guest_count"],
            sources=sorted(item["sources"]),
        ))

    ids = {e.id for e in events}
    if len(ids) != len(events):
        raise ValueError(
            f"Event ID hash collision detected: {len(events)} events but {len(ids)} unique IDs"
        )

    return events


def download_events(
    *, retries: int, start_utc: datetime, end_utc: datetime
) -> list[Event]:
    """Fetch events from all configured sources and return deduplicated list."""
    all_records: list[_EventRecord] = []

    category_slugs = [_extract_slug(url) for url in HARDCODED_CATEGORY_URLS]
    for slug in category_slugs:
        print(f"Fetching category events: {slug}", file=sys.stderr)
        all_records.extend(
            _fetch_category_events(slug, start_utc=start_utc, end_utc=end_utc, retries=retries)
        )

    for cal in HARDCODED_CALENDARS:
        slug = _extract_slug(cal["url"])
        calendar_api_id = cal.get("calendar_api_id")
        if calendar_api_id:
            print(f"Fetching calendar events: {slug} ({calendar_api_id})", file=sys.stderr)
            all_records.extend(
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
            print(
                f"Fetching calendar events: {slug} ({resolved_calendar_id})",
                file=sys.stderr,
            )
            all_records.extend(
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
            all_records.extend(
                _fetch_category_events(slug, start_utc=start_utc, end_utc=end_utc, retries=retries)
            )

    return _dedupe_by_url(all_records)
