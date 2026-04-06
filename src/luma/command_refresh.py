"""Refresh command – fetch events from all sources and write to cache."""

from __future__ import annotations

import pathlib
import sys
import urllib.error

from luma.event_store import EventStore
from luma.refresh import refresh
from luma.user_config import LLMConfig


def run(
    retries: int,
    store: EventStore,
    *,
    llm_config: LLMConfig | None,
    days: int | None = None,
    config_path: pathlib.Path | None = None,
    cache_dir: pathlib.Path | None = None,
) -> int:
    try:
        count = refresh(
            retries=retries,
            store=store,
            llm_config=llm_config,
            days=days,
            config_path=config_path,
            cache_dir=cache_dir,
        )
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"Error fetching events: {err}", file=sys.stderr)
        return 1
    print(f"Fetched {count} events", file=sys.stderr)
    return 0
