"""Refresh command – fetch events from all sources and write to cache."""

from __future__ import annotations

import pathlib
import sys
import urllib.error

from luma.event_store import EventStore
from luma.refresh import refresh
from luma.user_config import LLMConfig

_NEXT_QUERY_HINT_LINES = (
    "",
    "Example queries:",
    "  1. luma - show todays popular events",
    "  2. luma next-week --top 30",
    "",
    "See all commands and options:",
    "  luma --help",
)


def run(
    retries: int,
    store: EventStore,
    *,
    llm_config: LLMConfig | None,
    category_urls: list[str],
    calendars: list[dict[str, str | None]],
    latitude: str,
    longitude: str,
    days: int | None = None,
    config_path: pathlib.Path | None = None,
    cache_dir: pathlib.Path | None = None,
) -> int:
    try:
        count = refresh(
            retries=retries,
            store=store,
            llm_config=llm_config,
            category_urls=category_urls,
            calendars=calendars,
            latitude=latitude,
            longitude=longitude,
            days=days,
            config_path=config_path,
            cache_dir=cache_dir,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"Error fetching events: {err}", file=sys.stderr)
        return 1
    print(f"Fetched {count} events", file=sys.stderr)
    for line in _NEXT_QUERY_HINT_LINES:
        print(line, file=sys.stderr)
    return 0
