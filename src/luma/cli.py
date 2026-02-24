#!/usr/bin/env python3
"""Rank Luma events across hardcoded categories and calendars.

Spec implemented:
- Hardcoded categories: ai, tech
- Hardcoded calendars: genai-sf, sf, frontiertower (ID resolved if absent)
- Hardcoded geo context: lat=37.33939, lon=-121.89496
- Default sort by date (secondary rank by guest_count)
- Hardcoded dedupe by url
- Seen/discard: events marked via --discard are hidden on subsequent runs
- CLI:
  - --days (default 14), or --from-date/--to-date (YYYYMMDD)
  - --top (default 30)
  - --discard (mark displayed events as seen)
  - --all (show seen events grayed out)
  - --reset (clear seen state)
- Auto-pagination via next_cursor
- Retry/backoff + basic rate-limit handling
"""

from __future__ import annotations

import argparse
import fnmatch
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
CACHE_DIR = pathlib.Path.home() / ".cache" / "luma"
CACHE_STALE_HOURS = 12
FETCH_WINDOW_DAYS = 14
REQUEST_DELAY_SEC = 0.3
HARDCODED_CATEGORY_URLS = [
    "https://luma.com/ai",
    "https://luma.com/tech",
    "https://luma.com/sf"
]
HARDCODED_CALENDARS = [
    { "url": "https://luma.com/genai-sf", "calendar_api_id": "cal-JTdFQadEz0AOxyV" },
    { "url": "https://luma.com/frontiertower", "calendar_api_id": "cal-Sl7q1nHTRXQzjP2" },
    { "url": "https://luma.com/sf-hardware-meetup", "calendar_api_id": "cal-tFAzNGOZ9xn6kT2" },
    { "url": "https://luma.com/deepmind", "calendar_api_id": "cal-7Q5A70Bz5Idxopu" },
    { "url": "https://luma.com/genai-collective", "calendar_api_id": "cal-E74MDlDKBaeAwXK" },
    { "url": "https://luma.com/sfaiengineers", "calendar_api_id": "cal-EmYs2kgt1D9Gb27" },
]

SEEN_FILE = CACHE_DIR / "seen.json"

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


def extract_slug(url: str) -> str:
    path = urllib.parse.urlparse(url).path.strip("/")
    if not path:
        raise ValueError(f"Could not parse slug from URL: {url}")
    return path


def parse_iso8601_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_los_angeles_time(value: str) -> str:
    dt_la = parse_iso8601_utc(value).astimezone(ZoneInfo("America/Los_Angeles"))
    month = dt_la.strftime("%b")
    day = dt_la.day
    hour = dt_la.hour % 12 or 12
    ampm = "AM" if dt_la.hour < 12 else "PM"
    if dt_la.minute == 0:
        time_part = f"{hour}{ampm}"
    else:
        time_part = f"{hour}:{dt_la.minute:02d}{ampm}"
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    if dt_la.date() == today:
        weekday = "Today"
    else:
        weekday = dt_la.strftime("%a")
    return f"{weekday} {month} {day}, {time_part}"


def is_on_or_after_min_time(start_at: str, min_hour: int) -> bool:
    dt_la = parse_iso8601_utc(start_at).astimezone(ZoneInfo("America/Los_Angeles"))
    return dt_la.hour >= min_hour


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
            # Retry on rate-limit and transient server errors.
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
    """Resolve data source for a luma.com/<slug> URL.

    Returns:
      ("calendar", calendar_api_id) when the page is a calendar page.
      ("discover", None) when the page is a place/discover-style listing.
    """
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
        # Pages like /sf are discover/place pages without calendar object.
        return ("discover", None)

    # Fallback pattern if __NEXT_DATA__ parsing fails.
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
        # Keep earliest known start for stable display.
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


def find_latest_cache() -> pathlib.Path | None:
    """Return the newest events-*.json cache file, or None if no cache exists."""
    if not CACHE_DIR.is_dir():
        return None
    candidates = sorted(CACHE_DIR.glob("events-*.json"), reverse=True)
    if not candidates:
        return None
    return candidates[0]


def save_cache(events: list[dict[str, Any]], fetched_at: datetime) -> pathlib.Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": fetched_at.isoformat(),
        "events": events,
    }
    path = CACHE_DIR / _cache_filename(fetched_at)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def load_cache(path: pathlib.Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["events"]


def load_seen_urls() -> set[str]:
    if not SEEN_FILE.is_file():
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def save_seen_urls(urls: set[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def fetch_all_events(*, retries: int) -> list[dict[str, Any]]:
    """Fetch events from all sources for a FETCH_WINDOW_DAYS window and return deduped list."""
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
                fetch_category_events(
                    slug, start_utc=start_utc, end_utc=end_utc, retries=retries
                )
            )

    return dedupe_by_url(all_records)


def cmd_refresh(retries: int) -> int:
    """Fetch events from all sources and write to cache."""
    try:
        events = fetch_all_events(retries=retries)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"Error fetching events: {err}", file=sys.stderr)
        return 1
    fetched_at = datetime.now(timezone.utc)
    path = save_cache(events, fetched_at)
    print(f"Cached {len(events)} events to {path}", file=sys.stderr)
    return 0


def _add_query_args(parser: argparse.ArgumentParser) -> None:
    """Register query-related flags on *parser*."""
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Time window in days from now (default: 14). Mutually exclusive with --from-date/--to-date.",
    )
    parser.add_argument(
        "--from-date",
        default=None,
        metavar="YYYYMMDD",
        help="Start date for the event window (inclusive). Mutually exclusive with --days.",
    )
    parser.add_argument(
        "--to-date",
        default=None,
        metavar="YYYYMMDD",
        help="End date for the event window (inclusive). Mutually exclusive with --days.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=100,
        help="How many events to print after sorting (default: 100).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional output JSON path. If omitted, no file is written.",
    )
    parser.add_argument(
        "--sort",
        choices=["date", "guest"],
        default="date",
        help="Sort by event 'date' (default) or by 'guest'.",
    )
    parser.add_argument(
        "--min-guest",
        type=int,
        default=50,
        help="Minimum guest_count to include (default: 50).",
    )
    parser.add_argument(
        "--max-guest",
        type=int,
        default=None,
        help="Maximum guest_count to include (default: no limit).",
    )
    parser.add_argument(
        "--min-time",
        type=int,
        default=None,
        metavar="HOUR_0_23",
        help="Minimum event start hour in Los Angeles time (0-23). Example: 18.",
    )
    parser.add_argument(
        "--max-time",
        type=int,
        default=None,
        metavar="HOUR_0_23",
        help="Maximum event start hour in Los Angeles time (0-23). Example: 21.",
    )
    parser.add_argument(
        "--day",
        default=None,
        help="Comma-separated weekday filter (e.g. 'Tue,Thu'). Case-insensitive.",
    )
    parser.add_argument(
        "--exclude",
        default=None,
        help="Comma-separated keywords to exclude from titles (case-insensitive).",
    )
    parser.add_argument(
        "--search",
        default=None,
        help="Only show events whose title contains this keyword (case-insensitive). Mutually exclusive with --regex/--glob.",
    )
    parser.add_argument(
        "--regex",
        default=None,
        help="Only show events whose title matches this regex pattern (case-insensitive). Mutually exclusive with --search/--glob.",
    )
    parser.add_argument(
        "--glob",
        default=None,
        help="Only show events whose title matches this glob pattern (case-insensitive, e.g. '*AI*meetup*'). Mutually exclusive with --search/--regex.",
    )
    parser.add_argument(
        "--discard",
        action="store_true",
        help="Mark all displayed events as seen. Mutually exclusive with --all and --reset.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Show all events including previously discarded (seen ones are grayed out). Mutually exclusive with --discard.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear the seen events list. Mutually exclusive with --discard.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query and browse Luma events from a local cache.\n"
            "\n"
            "Sources:\n"
            "  Categories: ai, tech, sf\n"
            "  Calendars: genai-sf, frontiertower, sf-hardware-meetup, deepmind, genai-collective, sfaiengineers\n"
            "\n"
            "Subcommands:\n"
            "  luma refresh   Fetch events from all sources and write to cache.\n"
            "  luma [options] Query cached events (default).\n"
            "\n"
            f"Cache: {CACHE_DIR}/events-<timestamp>.json"
        ),
        epilog=(
            "Examples:\n"
            "  luma refresh\n"
            "    Fetch fresh events from all sources and save to cache.\n"
            "\n"
            "  luma\n"
            "    Show cached events with defaults (14 days, top 100, sort=date).\n"
            "\n"
            "  luma --days 7 --top 50\n"
            "    Show top 50 cached events in the next 7 days.\n"
            "\n"
            "  luma --sort guest --min-guest 100\n"
            "    Show cached events sorted by popularity, minimum 100 guests.\n"
            "\n"
            "  luma --search 'AI' --day Tue,Thu\n"
            "    Show cached events with 'AI' in the title on Tue/Thu.\n"
            "\n"
            "  luma refresh --retries 8\n"
            "    Fetch with more retries for flaky networks."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")
    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Fetch events from all sources and write to cache.",
    )
    refresh_parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Retry attempts for HTTP requests with exponential backoff (default: 5).",
    )
    _add_query_args(parser)
    return parser.parse_args(argv)


def cmd_query(args: argparse.Namespace) -> int:
    """Query cached events with filters and display results."""
    min_time_obj: int | None = None
    if args.min_time is not None:
        if args.min_time < 0 or args.min_time > 23:
            print("Invalid --min-time. Use an integer hour from 0 to 23.", file=sys.stderr)
            return 2
        min_time_obj = args.min_time
    max_time_obj: int | None = None
    if args.max_time is not None:
        if args.max_time < 0 or args.max_time > 23:
            print("Invalid --max-time. Use an integer hour from 0 to 23.", file=sys.stderr)
            return 2
        max_time_obj = args.max_time

    title_filter_count = sum(x is not None for x in [args.search, args.regex, args.glob])
    if title_filter_count > 1:
        print("--search, --regex, and --glob are mutually exclusive.", file=sys.stderr)
        return 2

    if args.discard and args.show_all:
        print("--discard and --all are mutually exclusive.", file=sys.stderr)
        return 2
    if args.discard and args.reset:
        print("--discard and --reset are mutually exclusive.", file=sys.stderr)
        return 2

    if args.reset:
        if SEEN_FILE.is_file():
            SEEN_FILE.unlink()
            print("Cleared seen events.", file=sys.stderr)
        else:
            print("No seen events to clear.", file=sys.stderr)

    regex_pattern: re.Pattern[str] | None = None
    if args.regex is not None:
        try:
            regex_pattern = re.compile(args.regex, re.IGNORECASE)
        except re.error as err:
            print(f"Invalid --regex pattern: {err}", file=sys.stderr)
            return 2

    day_name_to_weekday = {
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    }
    day_filter: set[int] | None = None
    if args.day:
        day_filter = set()
        for token in args.day.split(","):
            key = token.strip().lower()[:3]
            if key not in day_name_to_weekday:
                print(f"Unknown weekday: '{token.strip()}'. Use Mon,Tue,Wed,Thu,Fri,Sat,Sun.", file=sys.stderr)
                return 2
            day_filter.add(day_name_to_weekday[key])

    la_tz_parse = ZoneInfo("America/Los_Angeles")
    has_date_args = args.from_date is not None or args.to_date is not None
    if args.days is not None and has_date_args:
        print("--days cannot be used together with --from-date/--to-date.", file=sys.stderr)
        return 2

    now_utc = datetime.now(timezone.utc)
    today_la = now_utc.astimezone(la_tz_parse).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    if has_date_args:
        def _parse_date(raw: str, flag: str) -> datetime:
            try:
                return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=la_tz_parse)
            except ValueError:
                print(f"Invalid {flag} format: '{raw}'. Use YYYYMMDD.", file=sys.stderr)
                raise SystemExit(2)

        if args.from_date is not None:
            start_utc = _parse_date(args.from_date, "--from-date").astimezone(timezone.utc)
        else:
            start_utc = today_la.astimezone(timezone.utc)

        if args.to_date is not None:
            to_date_la = _parse_date(args.to_date, "--to-date")
            end_utc = (to_date_la + timedelta(days=1)).astimezone(timezone.utc)
        else:
            end_utc = start_utc + timedelta(days=FETCH_WINDOW_DAYS)

        if end_utc <= start_utc:
            print("--to-date cannot be earlier than --from-date.", file=sys.stderr)
            return 2
    else:
        days = args.days if args.days is not None else 14
        start_utc = today_la.astimezone(timezone.utc)
        end_utc = start_utc + timedelta(days=days)

    cache_path = find_latest_cache()
    if cache_path is None:
        print("No cached events. Run 'luma refresh' first.", file=sys.stderr)
        return 1

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cache_meta = json.load(f)
        fetched_at = parse_iso8601_utc(cache_meta["fetched_at"])
        cache_age = now_utc - fetched_at
        if cache_age > timedelta(hours=CACHE_STALE_HOURS):
            age_days = cache_age.days
            if age_days >= 1:
                print(f"Warning: cache is {age_days} day{'s' if age_days != 1 else ''} old. Run 'luma refresh' to update.", file=sys.stderr)
            else:
                age_hours = int(cache_age.total_seconds() // 3600)
                print(f"Warning: cache is {age_hours} hours old. Run 'luma refresh' to update.", file=sys.stderr)
    except (KeyError, json.JSONDecodeError, OSError):
        pass

    all_events = load_cache(cache_path)

    deduped = [
        item for item in all_events
        if start_utc <= parse_iso8601_utc(item["start_at"]) < end_utc
    ]
    deduped = [item for item in deduped if int(item["guest_count"]) >= args.min_guest]
    if args.max_guest is not None:
        deduped = [item for item in deduped if int(item["guest_count"]) <= args.max_guest]
    if min_time_obj is not None:
        deduped = [item for item in deduped if is_on_or_after_min_time(item["start_at"], min_time_obj)]
    if max_time_obj is not None:
        la_tz_max = ZoneInfo("America/Los_Angeles")
        deduped = [
            item for item in deduped
            if parse_iso8601_utc(item["start_at"]).astimezone(la_tz_max).hour <= max_time_obj
        ]
    if day_filter is not None:
        la_tz_filter = ZoneInfo("America/Los_Angeles")
        deduped = [
            item for item in deduped
            if parse_iso8601_utc(item["start_at"]).astimezone(la_tz_filter).weekday() in day_filter
        ]
    if args.exclude:
        exclude_keywords = [k.strip().lower() for k in args.exclude.split(",") if k.strip()]
        deduped = [
            item for item in deduped
            if not any(kw in item["title"].lower() for kw in exclude_keywords)
        ]
    if args.search:
        search_term = args.search.lower()
        deduped = [item for item in deduped if search_term in item["title"].lower()]
    if regex_pattern is not None:
        deduped = [item for item in deduped if regex_pattern.search(item["title"])]
    if args.glob is not None:
        glob_pat = args.glob.lower()
        deduped = [item for item in deduped if fnmatch.fnmatch(item["title"].lower(), glob_pat)]
    if args.sort == "date":
        la_tz = ZoneInfo("America/Los_Angeles")
        deduped.sort(
            key=lambda x: (
                parse_iso8601_utc(x["start_at"]).astimezone(la_tz).date(),
                -int(x["guest_count"]),
                x["title"].lower(),
            )
        )
    else:
        deduped.sort(
            key=lambda x: (
                -int(x["guest_count"]),
                parse_iso8601_utc(x["start_at"]),
                x["title"].lower(),
            )
        )

    seen_urls = load_seen_urls()
    if not args.show_all:
        deduped = [item for item in deduped if item["url"] not in seen_urls]

    output = {
        "generated_at": now_utc.isoformat(),
        "window_days": args.days if args.days is not None else (14 if not has_date_args else None),
        "from_date": args.from_date,
        "to_date": args.to_date,
        "window_start_utc": start_utc.isoformat(),
        "window_end_utc": end_utc.isoformat(),
        "rank_by": "guest_count",
        "sort": args.sort,
        "min_guest": args.min_guest,
        "max_guest": args.max_guest,
        "min_time": args.min_time,
        "max_time": args.max_time,
        "dedupe_by": "url",
        "lat": HARDCODED_LAT,
        "lon": HARDCODED_LON,
        "total_events_after_dedupe": len(deduped),
        "events": deduped,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)

    top_n = deduped[: args.top]
    print(f"Top {len(top_n)} events (sorted by {args.sort}):")
    score_width = max((len(f"[{int(item['guest_count'])}]") for item in top_n), default=3)
    date_width = max((len(format_los_angeles_time(item["start_at"])) for item in top_n), default=0)
    la_tz = ZoneInfo("America/Los_Angeles")
    bold = "\033[1m"
    dim = "\033[2m"
    reset_ansi = "\033[0m"
    highlight_days = {1, 3}
    prev_iso_week: tuple[int, int] | None = None
    for item in top_n:
        dt_la = parse_iso8601_utc(item["start_at"]).astimezone(la_tz)
        if args.sort == "date":
            iso_year, iso_week, _ = dt_la.isocalendar()
            current_week = (iso_year, iso_week)
            if prev_iso_week is not None and current_week != prev_iso_week:
                print()
            prev_iso_week = current_week

        start = format_los_angeles_time(item["start_at"])
        score = item["guest_count"]
        score_text = f"[{score}]".ljust(score_width)
        date_text = start.ljust(date_width)
        line = f"{score_text} {date_text} | {item['title']} | {item['url']}"
        if args.show_all and item["url"] in seen_urls:
            line = f"{dim}{line}{reset_ansi}"
        elif dt_la.weekday() in highlight_days:
            line = f"{bold}{line}{reset_ansi}"
        print(line)

    if args.discard:
        new_seen = seen_urls | {item["url"] for item in top_n}
        save_seen_urls(new_seen)
        print(f"Marked {len(top_n)} events as seen.", file=sys.stderr)

    if args.out:
        print(f"\nSaved full output: {args.out}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "refresh":
        return cmd_refresh(args.retries)
    return cmd_query(args)


if __name__ == "__main__":
    raise SystemExit(main())
