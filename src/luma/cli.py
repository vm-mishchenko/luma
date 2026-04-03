#!/usr/bin/env python3
"""CLI routing layer for Luma.

Parses arguments, constructs the EventStore, and dispatches to the appropriate
command module (command_query, command_refresh, command_chat).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

import luma.command_chat as command_chat
import luma.command_like as command_like
import luma.command_query as command_query
import luma.command_refresh as command_refresh
import luma.command_suggest as command_suggest
from luma.config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_LUMA_DIR,
    DEFAULT_RETRIES,
    DEFAULT_SORT,
    FETCH_WINDOW_DAYS,
    TIMEZONE_NAME,
)
from luma.event_store import EventStore
from luma.preference_store import DiskPreferenceProvider, PreferenceStore
from luma.user_config import (
    ensure_config,
    get_event_provider,
    get_llm_config,
    get_shortcuts,
    load_config,
    validate_config,
)


# ---------------------------------------------------------------------------
# Date subcommands
# ---------------------------------------------------------------------------

_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_FULL = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}

_DATE_SUBCMDS: set[str] = {
    "today", "tomorrow",
    "week", "weekday", "weekend",
    "next-week", "next-weekday", "next-weekend",
    *_DAY_NAMES.keys(),
    *(f"next-{d}" for d in _DAY_NAMES),
}

_CONFLICTING_DATE_FLAGS = {"--range", "--days", "--from-date", "--to-date"}


def _date_subcmd_to_range(name: str, today: date) -> tuple[date, date]:
    """Resolve a date subcommand name to (start_date, end_date) inclusive."""
    wd = today.weekday()  # Mon=0, Sun=6
    next_monday = today + timedelta(days=(7 - wd))

    if name == "today":
        return (today, today)
    if name == "tomorrow":
        t = today + timedelta(days=1)
        return (t, t)

    if name == "week":
        end = today + timedelta(days=(6 - wd))
        return (today, end)
    if name == "weekday":
        if wd >= 5:
            print("No weekdays left this week. Use 'next-weekday' for next Mon-Fri.", file=sys.stderr)
            raise SystemExit(1)
        end = today + timedelta(days=(4 - wd))
        return (today, end)
    if name == "weekend":
        if wd < 5:
            print("It's not the weekend. Use 'next-weekend' for the coming Sat-Sun.", file=sys.stderr)
            raise SystemExit(1)
        if wd == 5:
            return (today, today + timedelta(days=1))
        return (today, today)

    if name == "next-week":
        return (next_monday, next_monday + timedelta(days=6))
    if name == "next-weekday":
        return (next_monday, next_monday + timedelta(days=4))
    if name == "next-weekend":
        next_sat = next_monday + timedelta(days=5)
        return (next_sat, next_sat + timedelta(days=1))

    if name.startswith("next-"):
        day_key = name[5:]
        target_wd = _DAY_NAMES[day_key]
        target = next_monday + timedelta(days=target_wd)
        return (target, target)

    # Bare day name: mon, tue, ...
    target_wd = _DAY_NAMES[name]
    if wd > target_wd:
        full = _DAY_FULL[target_wd]
        print(
            f"{full} has already passed this week. Use 'next-{name}' for next {full}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    target = today + timedelta(days=(target_wd - wd))
    return (target, target)


_BOOLEAN_FLAGS = {"--json", "--debug", "--sf", "--discard", "--all", "--reset", "-h", "--help"}


def _resolve_date_subcmd(argv: list[str]) -> list[str]:
    """If the first positional in *argv* is a date subcommand, resolve it."""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg.startswith("-"):
            if "=" in arg:
                i += 1
                continue
            if arg in _BOOLEAN_FLAGS:
                i += 1
                continue
            # All other flags take a value
            i += 2
            continue
        # First positional found
        if arg not in _DATE_SUBCMDS:
            return list(argv)
        break
    else:
        return list(argv)

    if i >= len(argv) or argv[i] not in _DATE_SUBCMDS:
        return list(argv)

    name = argv[i]
    rest = argv[i + 1:]

    for flag in rest:
        bare = flag.split("=", 1)[0] if "=" in flag else flag
        if bare in _CONFLICTING_DATE_FLAGS:
            print(
                f"'{name}' date subcommand cannot be used with {bare}.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    la_tz = ZoneInfo(TIMEZONE_NAME)
    today_la = datetime.now(timezone.utc).astimezone(la_tz).date()
    start, end = _date_subcmd_to_range(name, today_la)

    before = list(argv[:i])
    resolved = before + [
        "--from-date", start.strftime("%Y%m%d"),
        "--to-date", end.strftime("%Y%m%d"),
    ] + rest

    clean = [a for a in resolved if a not in ("--config", "--cache-dir") and not a.startswith("--config=") and not a.startswith("--cache-dir=")]
    _dim = "\033[2m" if sys.stderr.isatty() else ""
    _reset = "\033[0m" if sys.stderr.isatty() else ""
    print(f"{_dim}luma {' '.join(clean)}{_reset}", file=sys.stderr)
    return resolved


def _load_env_local() -> None:
    """Load .env.local from the project root if it exists."""
    from dotenv import load_dotenv

    env_file = Path(__file__).resolve().parents[2] / ".env.local"
    load_dotenv(env_file, override=False)


def _add_query_args(parser: argparse.ArgumentParser, *, hidden: bool = False) -> None:
    """Register query-related flags on *parser*.

    When *hidden* is True the flags are still functional but suppressed from
    ``--help`` output (used on the main parser to keep top-level help clean).
    """
    def _h(text: str) -> str:
        return argparse.SUPPRESS if hidden else text

    parser.add_argument(
        "--days", type=int, default=None,
        help=_h("Time window in days from now (default: 1, today only). Mutually exclusive with --from-date/--to-date."),
    )
    parser.add_argument(
        "--from-date", default=None, metavar="YYYYMMDD",
        help=_h("Start date for the event window (inclusive). Mutually exclusive with --days."),
    )
    parser.add_argument(
        "--to-date", default=None, metavar="YYYYMMDD",
        help=_h("End date for the event window (inclusive). Mutually exclusive with --days."),
    )
    parser.add_argument(
        "--range", default=None, dest="range",
        help=_h("Predefined date range: today, tomorrow, week[+N], weekday[+N], weekend[+N]."),
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help=_h("Limit how many events to print after sorting (default: all)."),
    )
    parser.add_argument(
        "--sort", choices=["date", "guest"], default=DEFAULT_SORT,
        help=_h("Sort by event 'date' or by 'guest' (default)."),
    )
    parser.add_argument(
        "--min-guest", type=int, default=None,
        help=_h("Minimum guest_count to include."),
    )
    parser.add_argument(
        "--max-guest", type=int, default=None,
        help=_h("Maximum guest_count to include (default: no limit)."),
    )
    parser.add_argument(
        "--min-time", type=int, default=None, metavar="HOUR_0_23",
        help=_h("Minimum event start hour in Los Angeles time (0-23). Example: 18."),
    )
    parser.add_argument(
        "--max-time", type=int, default=None, metavar="HOUR_0_23",
        help=_h("Maximum event start hour in Los Angeles time (0-23). Example: 21."),
    )
    parser.add_argument(
        "--day", default=None,
        help=_h("Comma-separated weekday filter (e.g. 'Tue,Thu'). Case-insensitive."),
    )
    parser.add_argument(
        "--exclude", default=None,
        help=_h("Comma-separated keywords to exclude from titles (case-insensitive)."),
    )
    parser.add_argument(
        "--search", default=None,
        help=_h("Only show events whose title contains this keyword (case-insensitive). Mutually exclusive with --regex/--glob."),
    )
    parser.add_argument(
        "--regex", default=None,
        help=_h("Only show events whose title matches this regex pattern (case-insensitive). Mutually exclusive with --search/--glob."),
    )
    parser.add_argument(
        "--glob", default=None,
        help=_h("Only show events whose title matches this glob pattern (case-insensitive, e.g. '*AI*meetup*'). Mutually exclusive with --search/--regex."),
    )
    parser.add_argument(
        "--city", default=None,
        help=_h("Filter by city name (case-insensitive exact match)."),
    )
    parser.add_argument(
        "--region", default=None,
        help=_h("Filter by region/state (case-insensitive exact match)."),
    )
    parser.add_argument(
        "--country", default=None,
        help=_h("Filter by country (case-insensitive exact match)."),
    )
    parser.add_argument(
        "--location-type", default=None,
        help=_h("Filter by location type (e.g. 'offline', 'online')."),
    )
    parser.add_argument(
        "--sf", action="store_true",
        help=_h("Shortcut: filter by city 'San Francisco'. Overrides --city."),
    )
    parser.add_argument(
        "--lat", type=float, default=None,
        help=_h("Latitude of search center for proximity filter. Requires --lon."),
    )
    parser.add_argument(
        "--lon", type=float, default=None,
        help=_h("Longitude of search center for proximity filter. Requires --lat."),
    )
    parser.add_argument(
        "--radius", type=float, default=None,
        help=_h("Search radius in miles (default: 5). Requires --lat and --lon."),
    )
    parser.add_argument(
        "--discard", action="store_true",
        help=_h("Mark all displayed events as seen. Mutually exclusive with --all and --reset."),
    )
    parser.add_argument(
        "--all", action="store_true", dest="show_all",
        help=_h("Show all events including previously discarded (seen ones are grayed out). Mutually exclusive with --discard."),
    )
    parser.add_argument(
        "--reset", action="store_true",
        help=_h("Clear the seen events list. Mutually exclusive with --discard."),
    )


class _ParseRetry(Exception):
    pass


def _parse_with_query_text(
    parser: argparse.ArgumentParser, argv: list[str] | None
) -> argparse.Namespace:
    """Parse *argv* with fallback extraction of a trailing free-text query.

    Argparse subparsers consume the first positional as a subcommand. When that
    positional is actually free text (e.g. ``luma "hello"``), the normal parse
    fails. This helper catches that failure, extracts the trailing positional,
    and re-parses without it.
    """
    original_error = parser.error
    _caught: list[str] = []

    def _capture_error(message: str):  # noqa: ANN202
        _caught.append(message)
        raise _ParseRetry(message)

    # Attempt 1: standard parse (handles subcommands and flag-only queries).
    parser.error = _capture_error  # type: ignore[assignment]
    try:
        args = parser.parse_args(argv)
        args.query_text = None
        return args
    except SystemExit as exc:
        if exc.code == 0:
            raise
    except _ParseRetry:
        pass
    finally:
        parser.error = original_error  # type: ignore[assignment]

    # Attempt 2: extract trailing positional as free-text query.
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw and not raw[-1].startswith("-") and raw[-1] not in {"refresh", "chat", "sc", "like", "suggest", "query"}:
        candidate = raw[-1]
        rest = raw[:-1]
        parser.error = _capture_error  # type: ignore[assignment]
        try:
            args = parser.parse_args(rest)
            args.query_text = candidate
            return args
        except (_ParseRetry, SystemExit):
            pass
        finally:
            parser.error = original_error  # type: ignore[assignment]

    # Let the parser report the original error.
    parser.parse_args(argv)
    raise SystemExit(2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find your next Luma event.\n"
            "\n"
            "Subcommands:\n"
            "  luma           Show today's popular events.\n"
            "  luma refresh   Fetch fresh events from Luma.\n"
            "  luma query     Query events with filters.\n"
            "  luma like      Like or dislike events interactively.\n"
            "  luma suggest   Get personalized event suggestions.\n"
            "  luma sc        Run a saved shortcut.\n"
            "  luma chat      Interactive chat with Luma assistant.\n"
            "\n"
            "Date subcommands:\n"
            "  luma today|tomorrow              Events for today or tomorrow.\n"
            "  luma week|weekday|weekend        Remaining events this week/weekdays/weekend.\n"
            "  luma mon|tue|wed|thu|fri|sat|sun Events for that day this week.\n"
            "\n"
            "  Prefix any with 'next-' for the following week (e.g. next-week, next-fri).\n"
            "\n"
            "Configuration ~/.luma"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
        usage="luma [-h] [command] [options]",
    )
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--cache-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", dest="json_output", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--provider", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command")
    chat_parser = subparsers.add_parser("chat", help=argparse.SUPPRESS)
    chat_parser.add_argument("--provider", default=None, help=argparse.SUPPRESS)
    like_parser = subparsers.add_parser("like", help=argparse.SUPPRESS)
    _add_query_args(like_parser)
    query_parser = subparsers.add_parser("query", help=argparse.SUPPRESS)
    query_parser.add_argument("--provider", default=None, help=argparse.SUPPRESS)
    _add_query_args(query_parser)
    refresh_parser = subparsers.add_parser(
        "refresh",
        help=argparse.SUPPRESS,
    )
    refresh_parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retry attempts for HTTP requests with exponential backoff (default: {DEFAULT_RETRIES}).",
    )
    refresh_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"Number of days ahead to fetch events (default: {FETCH_WINDOW_DAYS}).",
    )
    refresh_parser.add_argument("--provider", default=None, help=argparse.SUPPRESS)
    subparsers.add_parser("sc", help=argparse.SUPPRESS)
    suggest_parser = subparsers.add_parser("suggest", help=argparse.SUPPRESS)
    suggest_parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Limit how many suggestions to return (default: 10).",
    )
    suggest_parser.add_argument("--provider", default=None, help=argparse.SUPPRESS)
    _add_query_args(parser, hidden=True)
    for grp in parser._action_groups:
        grp._group_actions = [a for a in grp._group_actions if not isinstance(a, argparse._SubParsersAction)]
    return _parse_with_query_text(parser, argv)


def _extract_global_flags(argv: list[str]) -> tuple[str | None, str | None, str | None]:
    """Scan *argv* for --config, --cache-dir, and --provider values without modifying it."""
    config_path: str | None = None
    cache_dir: str | None = None
    provider: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            config_path = argv[i + 1]
        elif arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
        elif arg == "--cache-dir" and i + 1 < len(argv):
            cache_dir = argv[i + 1]
        elif arg.startswith("--cache-dir="):
            cache_dir = arg.split("=", 1)[1]
        elif arg == "--provider" and i + 1 < len(argv):
            provider = argv[i + 1]
        elif arg.startswith("--provider="):
            provider = arg.split("=", 1)[1]
    return config_path, cache_dir, provider


def _resolve_sc(argv: list[str], config: dict, config_path: Path) -> list[str]:
    """If argv contains ``sc`` as the subcommand, resolve the shortcut."""
    # Find the subcommand position (first non-flag positional)
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg.startswith("-"):
            # Skip flags that consume a value
            if arg in ("--config", "--cache-dir") or (
                not arg.startswith("--") and len(arg) == 2
            ):
                i += 2
                continue
            if "=" in arg:
                i += 1
                continue
            # Boolean flags (--json, --debug, etc.)
            i += 1
            continue
        # First positional: check if it's "sc"
        if arg == "sc":
            break
        return list(argv)
        i += 1

    if i >= len(argv) or argv[i] != "sc":
        return list(argv)

    before_sc = argv[:i]
    after_sc = argv[i + 1:]
    shortcuts = get_shortcuts(config)

    # No name follows sc, or next arg is a flag → list shortcuts
    if not after_sc or after_sc[0].startswith("-"):
        if shortcuts:
            print("Available shortcuts:")
            for name, args in sorted(shortcuts.items()):
                print(f"  {name}: {' '.join(args)}")
        else:
            print("No shortcuts defined.")
        print(f"\nAdd shortcuts to {config_path}:")
        print('  [shortcuts]')
        print('  popular = ["--sort", "guest", "--min-guest", "100"]')
        print('  weekend = ["--range", "weekend"]')
        raise SystemExit(0)

    name = after_sc[0]
    extra = after_sc[1:]

    if name not in shortcuts:
        available = ", ".join(sorted(shortcuts)) if shortcuts else "(none)"
        print(
            f"Error: unknown shortcut '{name}'. Available: {available}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    resolved = before_sc + shortcuts[name] + extra
    clean = []
    skip_next = False
    for a in resolved:
        if skip_next:
            skip_next = False
            continue
        if a in ("--config", "--cache-dir"):
            skip_next = True
            continue
        if a.startswith("--config=") or a.startswith("--cache-dir="):
            continue
        clean.append(a)
    _dim = "\033[2m" if sys.stderr.isatty() else ""
    _reset = "\033[0m" if sys.stderr.isatty() else ""
    print(f"{_dim}luma {' '.join(clean)}{_reset}", file=sys.stderr)
    return resolved


def main() -> int:
    _load_env_local()
    raw_argv = sys.argv[1:]

    config_path_str, cache_dir_override, provider_override = _extract_global_flags(raw_argv)

    config_path = Path(config_path_str) if config_path_str else DEFAULT_CONFIG_PATH
    ensure_config(config_path)
    config = load_config(config_path)
    validate_config(config)

    argv = _resolve_sc(raw_argv, config, config_path)
    argv = _resolve_date_subcmd(argv)

    args = parse_args(argv)
    cache_dir = (
        Path(args.cache_dir).expanduser() if args.cache_dir else DEFAULT_CACHE_DIR
    )
    store = EventStore(get_event_provider(config, cache_dir))
    preferences = PreferenceStore(DiskPreferenceProvider(DEFAULT_LUMA_DIR))
    if args.json_output and args.command in ("refresh", "chat", "like", "suggest"):
        print(f"--json is not supported with '{args.command}'.", file=sys.stderr)
        return 2

    def _llm_config():
        return get_llm_config(config, provider_override=provider_override)

    if args.command == "refresh":
        return command_refresh.run(args.retries, store, llm_config=_llm_config(), days=args.days)
    if args.command == "chat":
        return command_chat.run(store, preferences, _llm_config())
    if args.command == "like":
        return command_like.run(args, store, preferences)
    if args.command == "suggest":
        return command_suggest.run(store, preferences, llm_config=_llm_config(), top=args.top)
    bare_form = args.command is None
    has_date_filter = args.from_date is not None or args.to_date is not None or args.days is not None or args.range is not None
    if bare_form and not has_date_filter and args.min_guest is None:
        args.min_guest = 1
    return command_query.run(args, store, cache_dir, preferences, _llm_config())


if __name__ == "__main__":
    raise SystemExit(main())
