"""Shared helpers for eval infrastructure."""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Any


def write_cache(
    tmp_path: pathlib.Path,
    events: list[dict[str, Any]],
    fetched_at: datetime | None = None,
) -> pathlib.Path:
    """Write a minimal cache file into *tmp_path*."""
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)
    stamp = fetched_at.strftime("%Y-%m-%d_%H-%M-%S")
    path = tmp_path / f"events-{stamp}.json"
    payload = {"fetched_at": fetched_at.isoformat(), "events": events}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
