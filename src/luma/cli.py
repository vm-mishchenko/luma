#!/usr/bin/env python3
"""CLI routing layer for Luma.

Parses arguments, constructs the EventStore, and dispatches to the appropriate
command module (command_query, command_refresh, command_chat).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import luma.command_chat as command_chat
import luma.command_like as command_like
import luma.command_query as command_query
import luma.command_refresh as command_refresh
import luma.command_suggest as command_suggest
from luma.config import (
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_CACHE_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_LUMA_DIR,
    DEFAULT_RETRIES,
    DEFAULT_SORT,
    FETCH_WINDOW_DAYS,
)
from luma.event_store import DiskProvider, EventStore
from luma.preference_store import DiskPreferenceProvider, PreferenceStore
from luma.user_config import (
    ensure_config,
    get_api_key,
    get_shortcuts,
    load_config,
    validate_config,
)


def _load_env_local() -> None:
    """Load .env.local from the project root if it exists."""
    from dotenv import load_dotenv

    env_file = Path(__file__).resolve().parents[2] / ".env.local"
    load_dotenv(env_file, override=False)


def _add_query_args(parser: argparse.ArgumentParser, *, suppress_help: bool = False) -> None:
    """Register query-related flags on *parser*."""
    S = argparse.SUPPRESS if suppress_help else None

    def _h(text: str) -> str:
        return argparse.SUPPRESS if suppress_help else text

    parser.add_argument(
        "--days", type=int, default=None,
        help=_h("Time window in days from now (default: 14). Mutually exclusive with --from-date/--to-date."),
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
        help=_h("Sort by event 'date' (default) or by 'guest'."),
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
    if raw and not raw[-1].startswith("-") and raw[-1] not in {"refresh", "chat", "sc", "like", "suggest"}:
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
            "Query and browse Luma events from a local cache.\n"
            "\n"
            "Subcommands:\n"
            "  luma refresh   Fetch events from all sources and write to cache.\n"
            "  luma chat      Interactive chat with Luma assistant.\n"
            "  luma like      Like or dislike events interactively.\n"
            "  luma suggest   Get personalized event suggestions.\n"
            "  luma [options] Query cached events (default).\n"
            "\n"
            "Configuration ~/.luma"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--cache-dir", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", dest="json_output", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", default=None, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command")
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
    subparsers.add_parser("chat", help=argparse.SUPPRESS)
    subparsers.add_parser("sc", help=argparse.SUPPRESS)
    like_parser = subparsers.add_parser("like", help=argparse.SUPPRESS)
    _add_query_args(like_parser)
    suggest_parser = subparsers.add_parser("suggest", help=argparse.SUPPRESS)
    suggest_parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Limit how many suggestions to return (default: 10).",
    )
    _add_query_args(parser, suppress_help=True)
    for action in parser._subparsers._group_actions:
        action.help = argparse.SUPPRESS
    return _parse_with_query_text(parser, argv)


def _extract_global_flags(argv: list[str]) -> tuple[str | None, str | None]:
    """Scan *argv* for --config and --cache-dir values without modifying it."""
    config_path: str | None = None
    cache_dir: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            config_path = argv[i + 1]
        elif arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
        elif arg == "--cache-dir" and i + 1 < len(argv):
            cache_dir = argv[i + 1]
        elif arg.startswith("--cache-dir="):
            cache_dir = arg.split("=", 1)[1]
    return config_path, cache_dir


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

    config_path_str, cache_dir_override = _extract_global_flags(raw_argv)

    config_path = Path(config_path_str) if config_path_str else DEFAULT_CONFIG_PATH
    ensure_config(config_path)
    config = load_config(config_path)
    validate_config(config)

    api_key = get_api_key(config)
    if api_key and not os.environ.get(ANTHROPIC_API_KEY_ENV):
        os.environ[ANTHROPIC_API_KEY_ENV] = api_key

    argv = _resolve_sc(raw_argv, config, config_path)

    args = parse_args(argv)
    cache_dir = (
        Path(args.cache_dir).expanduser() if args.cache_dir else DEFAULT_CACHE_DIR
    )
    store = EventStore(DiskProvider(cache_dir=cache_dir))
    preferences = PreferenceStore(DiskPreferenceProvider(DEFAULT_LUMA_DIR))
    if args.json_output and args.command in ("refresh", "chat", "like", "suggest"):
        print(f"--json is not supported with '{args.command}'.", file=sys.stderr)
        return 2
    if args.command == "refresh":
        return command_refresh.run(args.retries, store, days=args.days)
    if args.command == "chat":
        return command_chat.run(store, preferences)
    if args.command == "like":
        return command_like.run(args, store, preferences)
    if args.command == "suggest":
        return command_suggest.run(store, preferences, top=args.top)
    return command_query.run(args, store, cache_dir, preferences)


if __name__ == "__main__":
    raise SystemExit(main())
