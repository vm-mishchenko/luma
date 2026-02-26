"""Refresh command – fetch events from all sources and write to cache."""

from __future__ import annotations

import sys
import urllib.error

from luma.event_store import EventStore
from luma.refresh import refresh


def run(retries: int, store: EventStore) -> int:
    try:
        count = refresh(retries=retries, store=store)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"Error fetching events: {err}", file=sys.stderr)
        return 1
    print(f"Cached {count} events", file=sys.stderr)
    return 0
