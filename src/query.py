"""Reusable query library for filtering, sorting, and retrieving Luma events.

This module is the data-access and query layer between storage (cache files)
and consumers (CLI, future agent module).  It has no dependency on argparse,
produces no terminal output, and never imports from ``cli``.
"""

from __future__ import annotations

import fnmatch
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from zoneinfo import ZoneInfo

from config import DEFAULT_WINDOW_DAYS, EVENTS_CACHE_GLOB, TIMEZONE_NAME


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class QueryValidationError(ValueError):
    """Raised when query parameters are invalid."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class QueryParams:
    days: int | None = None
    from_date: str | None = None
    to_date: str | None = None
    min_guest: int = 50
    max_guest: int | None = None
    min_time: int | None = None
    max_time: int | None = None
    day: str | None = None
    exclude: str | None = None
    search: str | None = None
    regex: str | None = None
    glob: str | None = None
    sort: str = "date"
    show_all: bool = False


@dataclass
class QueryResult:
    events: list[dict[str, Any]]
    total_after_filter: int
    window_start_utc: datetime
    window_end_utc: datetime


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def parse_iso8601_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def is_on_or_after_min_time(start_at: str, min_hour: int) -> bool:
    dt_la = parse_iso8601_utc(start_at).astimezone(ZoneInfo(TIMEZONE_NAME))
    return dt_la.hour >= min_hour


# ---------------------------------------------------------------------------
# Cache access
# ---------------------------------------------------------------------------

def find_latest_cache(cache_dir: pathlib.Path) -> pathlib.Path | None:
    """Return the newest events-*.json cache file, or None if no cache exists."""
    if not cache_dir.is_dir():
        return None
    candidates = sorted(cache_dir.glob(EVENTS_CACHE_GLOB), reverse=True)
    if not candidates:
        return None
    return candidates[0]


def load_cache(path: pathlib.Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["events"]


# ---------------------------------------------------------------------------
# Query engine
# ---------------------------------------------------------------------------

def query_events(
    events: list[dict[str, Any]],
    params: QueryParams,
    *,
    seen_urls: set[str] | None = None,
) -> QueryResult:
    """Filter, sort, and return events.  Pure logic — no I/O."""

    # -- validation ----------------------------------------------------------

    if params.min_time is not None and not (0 <= params.min_time <= 23):
        raise QueryValidationError(
            "Invalid --min-time. Use an integer hour from 0 to 23."
        )
    if params.max_time is not None and not (0 <= params.max_time <= 23):
        raise QueryValidationError(
            "Invalid --max-time. Use an integer hour from 0 to 23."
        )

    title_filter_count = sum(
        x is not None for x in [params.search, params.regex, params.glob]
    )
    if title_filter_count > 1:
        raise QueryValidationError(
            "--search, --regex, and --glob are mutually exclusive."
        )

    regex_pattern: re.Pattern[str] | None = None
    if params.regex is not None:
        try:
            regex_pattern = re.compile(params.regex, re.IGNORECASE)
        except re.error as err:
            raise QueryValidationError(f"Invalid --regex pattern: {err}") from err

    day_name_to_weekday = {
        "mon": 0, "tue": 1, "wed": 2, "thu": 3,
        "fri": 4, "sat": 5, "sun": 6,
    }
    day_filter: set[int] | None = None
    if params.day:
        day_filter = set()
        for token in params.day.split(","):
            key = token.strip().lower()[:3]
            if key not in day_name_to_weekday:
                raise QueryValidationError(
                    f"Unknown weekday: '{token.strip()}'. "
                    "Use Mon,Tue,Wed,Thu,Fri,Sat,Sun."
                )
            day_filter.add(day_name_to_weekday[key])

    has_date_range = params.from_date is not None or params.to_date is not None
    if params.days is not None and has_date_range:
        raise QueryValidationError(
            "--days cannot be used together with --from-date/--to-date."
        )

    # -- date window ---------------------------------------------------------

    la_tz = ZoneInfo(TIMEZONE_NAME)
    now_utc = datetime.now(timezone.utc)
    today_la = now_utc.astimezone(la_tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    if has_date_range:
        def _parse_date(raw: str, label: str) -> datetime:
            try:
                return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=la_tz)
            except ValueError:
                raise QueryValidationError(
                    f"Invalid {label} format: '{raw}'. Use YYYYMMDD."
                )

        if params.from_date is not None:
            start_utc = _parse_date(
                params.from_date, "--from-date"
            ).astimezone(timezone.utc)
        else:
            start_utc = today_la.astimezone(timezone.utc)

        if params.to_date is not None:
            to_date_la = _parse_date(params.to_date, "--to-date")
            end_utc = (to_date_la + timedelta(days=1)).astimezone(timezone.utc)
        else:
            end_utc = start_utc + timedelta(days=DEFAULT_WINDOW_DAYS)

        if end_utc <= start_utc:
            raise QueryValidationError(
                "--to-date cannot be earlier than --from-date."
            )
    else:
        days = params.days if params.days is not None else DEFAULT_WINDOW_DAYS
        start_utc = today_la.astimezone(timezone.utc)
        end_utc = start_utc + timedelta(days=days)

    # -- filter chain --------------------------------------------------------

    filtered = [
        item for item in events
        if start_utc <= parse_iso8601_utc(item["start_at"]) < end_utc
    ]
    filtered = [
        item for item in filtered
        if int(item["guest_count"]) >= params.min_guest
    ]
    if params.max_guest is not None:
        filtered = [
            item for item in filtered
            if int(item["guest_count"]) <= params.max_guest
        ]
    if params.min_time is not None:
        filtered = [
            item for item in filtered
            if is_on_or_after_min_time(item["start_at"], params.min_time)
        ]
    if params.max_time is not None:
        filtered = [
            item for item in filtered
            if parse_iso8601_utc(item["start_at"]).astimezone(la_tz).hour
            <= params.max_time
        ]
    if day_filter is not None:
        filtered = [
            item for item in filtered
            if parse_iso8601_utc(item["start_at"]).astimezone(la_tz).weekday()
            in day_filter
        ]
    if params.exclude:
        exclude_keywords = [
            k.strip().lower() for k in params.exclude.split(",") if k.strip()
        ]
        filtered = [
            item for item in filtered
            if not any(kw in item["title"].lower() for kw in exclude_keywords)
        ]
    if params.search:
        search_term = params.search.lower()
        filtered = [
            item for item in filtered
            if search_term in item["title"].lower()
        ]
    if regex_pattern is not None:
        filtered = [
            item for item in filtered
            if regex_pattern.search(item["title"])
        ]
    if params.glob is not None:
        glob_pat = params.glob.lower()
        filtered = [
            item for item in filtered
            if fnmatch.fnmatch(item["title"].lower(), glob_pat)
        ]

    # -- sort ----------------------------------------------------------------

    if params.sort == "date":
        filtered.sort(
            key=lambda x: (
                parse_iso8601_utc(x["start_at"]).astimezone(la_tz).date(),
                -int(x["guest_count"]),
                x["title"].lower(),
            )
        )
    else:
        filtered.sort(
            key=lambda x: (
                -int(x["guest_count"]),
                parse_iso8601_utc(x["start_at"]),
                x["title"].lower(),
            )
        )

    # -- seen-URL exclusion (after sort, matching original order) -------------

    if seen_urls is not None and not params.show_all:
        filtered = [
            item for item in filtered if item["url"] not in seen_urls
        ]

    return QueryResult(
        events=filtered,
        total_after_filter=len(filtered),
        window_start_utc=start_utc,
        window_end_utc=end_utc,
    )
