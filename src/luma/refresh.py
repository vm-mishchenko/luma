"""Refresh orchestration for Luma events.

This module coordinates downloading and caching. Network I/O is delegated
to ``download.download_events``; cache paths come from ``config``.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import luma.config as config
from luma.config import EVENTS_FILENAME_PREFIX, FETCH_WINDOW_DAYS, TIMEZONE_NAME
from luma.download import download_events


def _window(now_utc: datetime) -> tuple[datetime, datetime]:
    today_la = now_utc.astimezone(ZoneInfo(TIMEZONE_NAME)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_utc = today_la.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=FETCH_WINDOW_DAYS)
    return start_utc, end_utc


def _cache_filename(fetched_at: datetime) -> str:
    stamp = fetched_at.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{EVENTS_FILENAME_PREFIX}{stamp}.json"


def save_cache(
    events: list[dict[str, Any]], fetched_at: datetime, cache_dir: pathlib.Path
) -> pathlib.Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": fetched_at.isoformat(),
        "events": events,
    }
    path = cache_dir / _cache_filename(fetched_at)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def refresh(*, retries: int) -> tuple[int, pathlib.Path]:
    """Fetch all events and write to cache. Returns (event_count, cache_path)."""
    cache_dir = config.get_cache_dir()
    now_utc = datetime.now(timezone.utc)
    start_utc, end_utc = _window(now_utc)
    events = download_events(retries=retries, start_utc=start_utc, end_utc=end_utc)
    path = save_cache(events, fetched_at=now_utc, cache_dir=cache_dir)
    return len(events), path
