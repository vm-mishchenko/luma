"""Refresh orchestration for Luma events.

This module coordinates downloading and saving.  Network I/O is delegated
to ``download.download_events``; storage is delegated to ``EventStore``.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from luma.config import (
    FETCH_WINDOW_DAYS,
    HARDCODED_CALENDARS,
    HARDCODED_CATEGORY_URLS,
    TIMEZONE_NAME,
)
from luma.download import download_events
from luma.enrich import enrich_events
from luma.event_store import EventStore


def _window(now_utc: datetime, days: int) -> tuple[datetime, datetime]:
    today_la = now_utc.astimezone(ZoneInfo(TIMEZONE_NAME)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = today_la.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=days)
    return start_utc, end_utc


def refresh(
    *, retries: int, store: EventStore, days: int | None = None
) -> tuple[int, pathlib.Path | None]:
    """Fetch all events and save via store. Returns (count, cache_path)."""
    now_utc = datetime.now(timezone.utc)
    window_days = days or FETCH_WINDOW_DAYS
    start_utc, end_utc = _window(now_utc, days=window_days)
    print(
        f"Fetching events for the next {window_days} days"
        f" from {len(HARDCODED_CATEGORY_URLS)} categories and {len(HARDCODED_CALENDARS)} calendars",
        file=sys.stderr,
    )
    events = download_events(retries=retries, start_utc=start_utc, end_utc=end_utc)
    events = enrich_events(events)
    path = store.save(events, now_utc)
    return len(events), path
