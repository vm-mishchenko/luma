"""Refresh orchestration for Luma events.

This module coordinates downloading and saving.  Network I/O is delegated
to ``download.download_events``; storage is delegated to ``EventStore``.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from luma.config import FETCH_WINDOW_DAYS, TIMEZONE_NAME
from luma.download import download_events
from luma.enrich import enrich_events
from luma.event_store import EventStore
from luma.user_config import LLMConfig


def _window(now_utc: datetime, days: int) -> tuple[datetime, datetime]:
    today_la = now_utc.astimezone(ZoneInfo(TIMEZONE_NAME)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = today_la.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=days)
    return start_utc, end_utc


def refresh(
    *,
    retries: int,
    store: EventStore,
    llm_config: LLMConfig | None,
    category_urls: list[str],
    calendars: list[dict[str, str | None]],
    days: int | None = None,
    config_path: pathlib.Path | None = None,
    cache_dir: pathlib.Path | None = None,
) -> int:
    """Fetch all events and upsert into store. Returns fetch count."""
    now_utc = datetime.now(timezone.utc)
    window_days = days or FETCH_WINDOW_DAYS
    start_utc, end_utc = _window(now_utc, days=window_days)
    print(
        f"Fetching events for the next {window_days} days"
        f" from {len(category_urls)} categories and {len(calendars)} calendars",
        file=sys.stderr,
    )
    events = download_events(
        retries=retries,
        start_utc=start_utc,
        end_utc=end_utc,
        category_urls=category_urls,
        calendars=calendars,
    )
    events = enrich_events(
        events, llm_config, config_path=config_path, cache_dir=cache_dir
    )
    store.upsert(events)
    return len(events)
