"""Refresh command – fetch events from all sources and write to cache."""

from __future__ import annotations

import sys
import urllib.error

from luma.refresh import refresh


def run(retries: int) -> int:
    try:
        count, path = refresh(retries=retries)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as err:
        print(f"Error fetching events: {err}", file=sys.stderr)
        return 1
    print(f"Cached {count} events to {path}", file=sys.stderr)
    return 0
