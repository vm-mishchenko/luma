"""Refresh orchestration for Luma events.

This module coordinates downloading and saving.  Network I/O is delegated
to ``download.download_events``; storage is delegated to ``EventStore``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from luma.config import FETCH_WINDOW_DAYS, TIMEZONE_NAME
from luma.download import download_events
from luma.event_store import EventStore


def _window(now_utc: datetime) -> tuple[datetime, datetime]:
    today_la = now_utc.astimezone(ZoneInfo(TIMEZONE_NAME)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = today_la.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=FETCH_WINDOW_DAYS)
    return start_utc, end_utc


def refresh(*, retries: int, store: EventStore) -> int:
    """Fetch all events and save via store. Returns event count."""
    now_utc = datetime.now(timezone.utc)
    start_utc, end_utc = _window(now_utc)
    events = download_events(retries=retries, start_utc=start_utc, end_utc=end_utc)
    store.save(events, now_utc)
    return len(events)
