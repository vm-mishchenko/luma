"""Query command – display cached events or route free-text through the Agent."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from luma.config import (
    DEFAULT_WINDOW_DAYS,
    HARDCODED_LAT,
    HARDCODED_LON,
    SEEN_FILENAME,
    TIMEZONE_NAME,
)
from luma.event_store import (
    CacheError,
    EventStore,
    QueryParams,
    QueryValidationError,
    parse_iso8601_utc,
)
from luma.models import Event

_DIM = "\033[2m" if sys.stderr.isatty() else ""
_RESET = "\033[0m" if sys.stderr.isatty() else ""


class _Loader:
    """Spinner with label, writes to stderr. Suppressed when not a TTY."""

    _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._label = ""

    def start(self, label: str) -> None:
        if not sys.stdout.isatty():
            return
        self._label = label
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        idx = 0
        while not self._stop_event.is_set():
            frame = self._FRAMES[idx % len(self._FRAMES)]
            print(f"\r{frame} {self._label} ", end="", file=sys.stderr, flush=True)
            idx += 1
            time.sleep(0.08)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join()
        self._thread = None
        if sys.stdout.isatty():
            # Clear the line (EL: erase from cursor to end)
            print("\r\033[K", end="", file=sys.stderr, flush=True)


def _format_los_angeles_time(value: str) -> str:
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


def _load_seen_urls(cache_dir: pathlib.Path) -> set[str]:
    seen_file = cache_dir / SEEN_FILENAME
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


def _save_seen_urls(urls: set[str], cache_dir: pathlib.Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    seen_file = cache_dir / SEEN_FILENAME
    with open(seen_file, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=2)


def _build_query_params(args: argparse.Namespace) -> QueryParams:
    return QueryParams(
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


_PARAM_TO_FLAG = {
    "days": "--days",
    "from_date": "--from-date",
    "to_date": "--to-date",
    "min_guest": "--min-guest",
    "max_guest": "--max-guest",
    "min_time": "--min-time",
    "max_time": "--max-time",
    "day": "--day",
    "sort": "--sort",
}


def _params_to_cli_flags(params: QueryParams) -> str:
    parts = []
    for field, flag in _PARAM_TO_FLAG.items():
        value = getattr(params, field, None)
        if value is not None:
            parts.append(f"{flag} {value}")
    return " ".join(parts)


def _print_events(
    events: list[Event],
    *,
    sort: str,
    show_all: bool = False,
    seen_urls: set[str] | None = None,
) -> None:
    print(f"Top {len(events)} events (sorted by {sort}):")
    score_width = max(
        (len(f"[{item.guest_count}]") for item in events), default=3
    )
    date_width = max(
        (len(_format_los_angeles_time(item.start_at)) for item in events),
        default=0,
    )
    la_tz = ZoneInfo(TIMEZONE_NAME)
    bold = "\033[1m"
    dim = "\033[2m"
    reset_ansi = "\033[0m"
    highlight_days = {1, 3}
    prev_iso_week: tuple[int, int] | None = None
    for item in events:
        dt_la = parse_iso8601_utc(item.start_at).astimezone(la_tz)
        if sort == "date":
            iso_year, iso_week, _ = dt_la.isocalendar()
            current_week = (iso_year, iso_week)
            if prev_iso_week is not None and current_week != prev_iso_week:
                print()
            prev_iso_week = current_week

        start = _format_los_angeles_time(item.start_at)
        score = item.guest_count
        score_text = f"[{score}]".ljust(score_width)
        date_text = start.ljust(date_width)
        line = f"{score_text} {date_text} | {item.title} | {item.url}"
        if show_all and seen_urls and item.url in seen_urls:
            line = f"{dim}{line}{reset_ansi}"
        elif dt_la.weekday() in highlight_days:
            line = f"{bold}{line}{reset_ansi}"
        print(line)


def _query(
    args: argparse.Namespace,
    store: EventStore,
    cache_dir: pathlib.Path,
) -> int:
    if args.discard and args.show_all:
        print("--discard and --all are mutually exclusive.", file=sys.stderr)
        return 2
    if args.discard and args.reset:
        print("--discard and --reset are mutually exclusive.", file=sys.stderr)
        return 2

    if args.reset:
        seen_file = cache_dir / SEEN_FILENAME
        if seen_file.is_file():
            seen_file.unlink()
            print("Cleared seen events.", file=sys.stderr)
        else:
            print("No seen events to clear.", file=sys.stderr)

    staleness = store.check_staleness()
    if staleness.is_stale:
        age_days = staleness.age.days
        if age_days >= 1:
            print(f"Warning: cache is {age_days} day{'s' if age_days != 1 else ''} old. "
                  "Run 'luma refresh' to update.", file=sys.stderr)
        else:
            age_hours = int(staleness.age.total_seconds() // 3600)
            print(f"Warning: cache is {age_hours} hours old. "
                  "Run 'luma refresh' to update.", file=sys.stderr)

    seen_urls = _load_seen_urls(cache_dir)

    params = _build_query_params(args)
    try:
        result = store.query(params, seen_urls=seen_urls)
    except CacheError as e:
        print(str(e), file=sys.stderr)
        return 1
    except QueryValidationError as e:
        print(str(e), file=sys.stderr)
        return 2

    now_utc = datetime.now(timezone.utc)
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
        "events": [e.to_dict() for e in result.events],
    }
    if args.json_output:
        output["type"] = "query"
        print(json.dumps(output, indent=2))
        if args.discard:
            new_seen = seen_urls | {e.url for e in result.events}
            _save_seen_urls(new_seen, cache_dir)
        return 0

    display = result.events[: args.top] if args.top else result.events
    _print_events(display, sort=args.sort, show_all=args.show_all, seen_urls=seen_urls)

    if args.discard:
        new_seen = seen_urls | {e.url for e in display}
        _save_seen_urls(new_seen, cache_dir)
        print(f"Marked {len(display)} events as seen.", file=sys.stderr)

    return 0


def _agent_query(args: argparse.Namespace, store: EventStore) -> int:
    from luma.agent import (
        Agent,
        AgentError,
        EventListResult,
        FinalResult,
        QueryParamsResult,
        TextOutput,
        TextResult,
    )

    debug = getattr(args, "debug", False)
    params = _build_query_params(args)
    agent = Agent(store=store, debug=debug)

    if args.json_output:
        try:
            result = agent.query(args.query_text, params)
        except AgentError as exc:
            print(f"Agent error: {exc}", file=sys.stderr)
            return 1
        if isinstance(result, TextResult):
            print(json.dumps({"type": "text", "text": result.text}, indent=2))
        elif isinstance(result, EventListResult):
            events = store.get_by_ids(result.ids)
            if debug and len(events) < len(result.ids):
                missing = len(result.ids) - len(events)
                print(f"[debug] {missing} event ID(s) not found in store", file=sys.stderr)
            print(json.dumps({
                "type": "events",
                "events": [e.to_dict() for e in events],
                "total": len(events),
            }, indent=2))
        elif isinstance(result, QueryParamsResult):
            query_result = store.query(result.params)
            print(json.dumps({
                "type": "events",
                "events": [e.to_dict() for e in query_result.events],
                "total": len(query_result.events),
            }, indent=2))
        return 0

    loader = _Loader()
    try:
        for item in agent.query_iter(args.query_text, params, loader=loader):
            if isinstance(item, TextOutput):
                print(f"{_DIM}{item.text}{_RESET}", file=sys.stderr)
            elif isinstance(item, FinalResult):
                result = item.result
                if isinstance(result, TextResult):
                    if result.text:
                        print(result.text)
                elif isinstance(result, EventListResult):
                    events = store.get_by_ids(result.ids)
                    if debug and len(events) < len(result.ids):
                        missing = len(result.ids) - len(events)
                        print(f"[debug] {missing} event ID(s) not found in store", file=sys.stderr)
                    display = events[: args.top] if args.top else events
                    _print_events(display, sort=args.sort)
                elif isinstance(result, QueryParamsResult):
                    cli_flags = _params_to_cli_flags(result.params)
                    print(f"{_DIM}luma {cli_flags}{_RESET}", file=sys.stderr)
                    query_result = store.query(result.params)
                    display = query_result.events[: args.top] if args.top else query_result.events
                    _print_events(
                        display,
                        sort=result.params.sort or args.sort,
                    )
    except AgentError as exc:
        loader.stop()
        print(f"Agent error: {exc}", file=sys.stderr)
        return 1

    return 0


def run(
    args: argparse.Namespace,
    store: EventStore,
    cache_dir: pathlib.Path,
) -> int:
    if args.query_text:
        return _agent_query(args, store)
    return _query(args, store, cache_dir)
