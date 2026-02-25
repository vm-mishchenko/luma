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
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from chat import cmd_chat
from config import (
    CACHE_STALE_HOURS,
    DEFAULT_WINDOW_DAYS,
    HARDCODED_LAT,
    HARDCODED_LON,
    TIMEZONE_NAME,
)
from query import (
    QueryParams,
    QueryValidationError,
    find_latest_cache as _q_find_latest_cache,
    load_cache,
    parse_iso8601_utc,
    query_events,
)
from refresh import refresh


def format_los_angeles_time(value: str) -> str:
    dt_la = parse_iso8601_utc(value).astimezone(ZoneInfo(TIMEZONE_NAME))
    month = dt_la.strftime("%b")
    day = dt_la.day
    hour = dt_la.hour % 12 or 12
    ampm = "AM" if dt_la.hour < 12 else "PM"
    if dt_la.minute == 0:
        time_part = f"{hour}{ampm}"
    else:
        time_part = f"{hour}:{dt_la.minute:02d}{ampm}"
    today = datetime.now(ZoneInfo(TIMEZONE_NAME)).date()
    if dt_la.date() == today:
        weekday = "Today"
    else:
        weekday = dt_la.strftime("%a")
    return f"{weekday} {month} {day}, {time_part}"


def find_latest_cache():
    """Return the newest events-*.json cache file, or None if no cache exists."""
    return _q_find_latest_cache(config.get_cache_dir())


def load_seen_urls() -> set[str]:
    seen_file = config.get_seen_file()
    if not seen_file.is_file():
        return set()
    try:
        with open(seen_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def save_seen_urls(urls: set[str]) -> None:
    cache_dir = config.get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(config.get_seen_file(), "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def cmd_refresh(retries: int) -> int:
    """Fetch events from all sources and write to cache."""
    try:
        count, path = refresh(retries=retries)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"Error fetching events: {err}", file=sys.stderr)
        return 1
    print(f"Cached {count} events to {path}", file=sys.stderr)
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
            "  luma chat      Interactive chat with Luma assistant.\n"
            "  luma [options] Query cached events (default).\n"
            "\n"
            "Cache: <cache-dir>/events-<timestamp>.json"
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
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Override the cache directory (default: ~/.cache/luma).",
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
    subparsers.add_parser(
        "chat",
        help="Interactive chat with Luma assistant.",
    )
    _add_query_args(parser)
    return parser.parse_args(argv)


def cmd_query(args: argparse.Namespace) -> int:
    """Query cached events with filters and display results."""
    if args.discard and args.show_all:
        print("--discard and --all are mutually exclusive.", file=sys.stderr)
        return 2
    if args.discard and args.reset:
        print("--discard and --reset are mutually exclusive.", file=sys.stderr)
        return 2

    if args.reset:
        seen_file = config.get_seen_file()
        if seen_file.is_file():
            seen_file.unlink()
            print("Cleared seen events.", file=sys.stderr)
        else:
            print("No seen events to clear.", file=sys.stderr)

    cache_path = find_latest_cache()
    if cache_path is None:
        print("No cached events. Run 'luma refresh' first.", file=sys.stderr)
        return 1

    now_utc = datetime.now(timezone.utc)
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
    seen_urls = load_seen_urls()

    params = QueryParams(
        days=args.days,
        from_date=args.from_date,
        to_date=args.to_date,
        min_guest=args.min_guest,
        max_guest=args.max_guest,
        min_time=args.min_time,
        max_time=args.max_time,
        day=args.day,
        exclude=args.exclude,
        search=args.search,
        regex=args.regex,
        glob=args.glob,
        sort=args.sort,
        show_all=args.show_all,
    )
    try:
        result = query_events(all_events, params, seen_urls=seen_urls)
    except QueryValidationError as e:
        print(str(e), file=sys.stderr)
        return 2

    has_date_args = args.from_date is not None or args.to_date is not None
    output = {
        "generated_at": now_utc.isoformat(),
        "window_days": args.days
        if args.days is not None
        else (DEFAULT_WINDOW_DAYS if not has_date_args else None),
        "from_date": args.from_date,
        "to_date": args.to_date,
        "window_start_utc": result.window_start_utc.isoformat(),
        "window_end_utc": result.window_end_utc.isoformat(),
        "rank_by": "guest_count",
        "sort": args.sort,
        "min_guest": args.min_guest,
        "max_guest": args.max_guest,
        "min_time": args.min_time,
        "max_time": args.max_time,
        "dedupe_by": "url",
        "lat": HARDCODED_LAT,
        "lon": HARDCODED_LON,
        "total_events_after_dedupe": result.total_after_filter,
        "events": result.events,
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2)

    top_n = result.events[: args.top]
    print(f"Top {len(top_n)} events (sorted by {args.sort}):")
    score_width = max((len(f"[{int(item['guest_count'])}]") for item in top_n), default=3)
    date_width = max((len(format_los_angeles_time(item["start_at"])) for item in top_n), default=0)
    la_tz = ZoneInfo(TIMEZONE_NAME)
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
    config.configure(cache_dir=args.cache_dir)
    if args.command == "refresh":
        return cmd_refresh(args.retries)
    if args.command == "chat":
        return cmd_chat()
    return cmd_query(args)


if __name__ == "__main__":
    raise SystemExit(main())
