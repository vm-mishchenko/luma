"""Refresh command – fetch events from all sources and write to cache."""

from __future__ import annotations

import sys
import urllib.error

from luma.event_store import EventStore
from luma.refresh import refresh


def run(retries: int, store: EventStore, *, days: int | None = None) -> int:
    try:
        count, path = refresh(retries=retries, store=store, days=days)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"Error fetching events: {err}", file=sys.stderr)
        return 1
    location = f" in {path}" if path else ""
    print(f"Cached {count} events{location}, ready for search", file=sys.stderr)
    return 0
