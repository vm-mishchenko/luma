#!/usr/bin/env python3
"""CLI routing layer for Luma.

Parses arguments, constructs the EventStore, and dispatches to the appropriate
command module (command_query, command_refresh, command_chat).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import luma.command_chat as command_chat
import luma.command_query as command_query
import luma.command_refresh as command_refresh
from luma.config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_RETRIES,
    DEFAULT_SORT,
)
from luma.event_store import DiskProvider, EventStore


def _load_env_local() -> None:
    """Load .env.local from the project root if it exists."""
    from dotenv import load_dotenv

    env_file = Path(__file__).resolve().parents[2] / ".env.local"
    load_dotenv(env_file, override=False)


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
        default=None,
        help="Limit how many events to print after sorting (default: all).",
    )
    parser.add_argument(
        "--sort",
        choices=["date", "guest"],
        default=DEFAULT_SORT,
        help=f"Sort by event 'date' (default) or by 'guest'.",
    )
    parser.add_argument(
        "--min-guest",
        type=int,
        default=None,
        help="Minimum guest_count to include.",
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
    except (_ParseRetry, SystemExit):
        pass
    finally:
        parser.error = original_error  # type: ignore[assignment]

    # Attempt 2: extract trailing positional as free-text query.
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw and not raw[-1].startswith("-") and raw[-1] not in {"refresh", "chat"}:
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
            "    Show all cached events with defaults (14 days, sort=date).\n"
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
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output structured JSON to stdout instead of human-readable text.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (e.g. agent tool call params).",
    )
    subparsers = parser.add_subparsers(dest="command")
    refresh_parser = subparsers.add_parser(
        "refresh",
        help="Fetch events from all sources and write to cache.",
    )
    refresh_parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retry attempts for HTTP requests with exponential backoff (default: {DEFAULT_RETRIES}).",
    )
    subparsers.add_parser(
        "chat",
        help="Interactive chat with Luma assistant.",
    )
    _add_query_args(parser)
    return _parse_with_query_text(parser, argv)


def main() -> int:
    _load_env_local()
    args = parse_args()
    cache_dir = (
        Path(args.cache_dir).expanduser() if args.cache_dir else DEFAULT_CACHE_DIR
    )
    store = EventStore(DiskProvider(cache_dir=cache_dir))
    if args.json_output and args.command in ("refresh", "chat"):
        print(f"--json is not supported with '{args.command}'.", file=sys.stderr)
        return 2
    if args.command == "refresh":
        return command_refresh.run(args.retries, store)
    if args.command == "chat":
        return command_chat.run(store)
    return command_query.run(args, store, cache_dir)


if __name__ == "__main__":
    raise SystemExit(main())
