"""EventStore — unified abstraction for reading and writing Luma events.

Providers handle storage mechanics (disk or memory).  Callers construct a
provider, pass it to ``EventStore``, and interact only with the store after
that.
"""

from __future__ import annotations

import fnmatch
import json
import math
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo

from luma.config import (
    CACHE_STALE_HOURS,
    DEFAULT_SEARCH_RADIUS_MILES,
    DEFAULT_SORT,
    DEFAULT_WINDOW_DAYS,
    EVENTS_CACHE_GLOB,
    EVENTS_FILENAME_PREFIX,
    TIMEZONE_NAME,
)
from luma.models import Event


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class QueryValidationError(ValueError):
    """Raised when query parameters are invalid."""


class CacheError(Exception):
    """Raised when the event cache is missing or corrupt."""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class QueryParams(BaseModel):
    days: int | None = Field(None, description="Window size in days starting from today. days=1 means today only, days=2 means today and tomorrow, etc. For a specific date use from_date/to_date instead. Mutually exclusive with from_date/to_date.")
    from_date: str | None = Field(None, description="Start date in YYYYMMDD format (inclusive). Mutually exclusive with days.")
    to_date: str | None = Field(None, description="End date in YYYYMMDD format (inclusive). Mutually exclusive with days.")
    min_guest: int | None = Field(None, description="Minimum guest count to include.")
    max_guest: int | None = Field(None, description="Maximum guest count to include.")
    min_time: int | None = Field(None, description="Minimum event start hour in Los Angeles time (0-23).")
    max_time: int | None = Field(None, description="Maximum event start hour in Los Angeles time (0-23).")
    day: str | None = Field(None, description="Comma-separated weekday filter, e.g. 'Sat,Sun'. Case-insensitive.")
    exclude: str | None = Field(None, description="Comma-separated keywords to exclude from titles (case-insensitive).")
    search: str | None = Field(None, description="Keyword search in event titles (case-insensitive). Mutually exclusive with regex and glob.")
    regex: str | None = Field(None, description="Regex pattern to match event titles (case-insensitive). Mutually exclusive with search and glob.")
    glob: str | None = Field(None, description="Glob pattern to match event titles (case-insensitive, e.g. '*AI*meetup*'). Mutually exclusive with search and regex.")
    sort: Literal["date", "guest"] = Field(DEFAULT_SORT, description="Sort by event date (default) or guest count.")
    show_all: bool = False
    city: str | None = Field(None, description="Filter by city name (case-insensitive exact match). Mutually exclusive with search_lat/search_lon.")
    region: str | None = Field(None, description="Filter by region/state (case-insensitive exact match).")
    country: str | None = Field(None, description="Filter by country (case-insensitive exact match).")
    location_type: str | None = Field(None, description="Filter by location type, e.g. 'offline', 'online'.")
    search_lat: float | None = Field(None, description="Latitude of search center for proximity filter. Requires search_lon. Mutually exclusive with city.")
    search_lon: float | None = Field(None, description="Longitude of search center for proximity filter. Requires search_lat. Mutually exclusive with city.")
    search_radius_miles: float | None = Field(None, description="Search radius in miles. Requires search_lat and search_lon.")


@dataclass
class CacheInfo:
    is_stale: bool
    age: timedelta


@dataclass
class QueryResult:
    events: list[Event]
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


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Provider protocol & implementations
# ---------------------------------------------------------------------------

class EventProvider(Protocol):
    def load(self) -> list[Event]: ...
    def save(self, events: list[Event], fetched_at: datetime) -> None: ...
    def check_staleness(self) -> CacheInfo: ...


class DiskProvider:
    """Reads and writes events as JSON cache files on disk."""

    def __init__(self, cache_dir: pathlib.Path) -> None:
        self._cache_dir = cache_dir

    def load(self) -> list[Event]:
        path = self._find_latest_cache()
        if path is None:
            raise CacheError("No cached events. Run 'luma refresh' first.")
        return self._load_cache(path)

    def save(self, events: list[Event], fetched_at: datetime) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = fetched_at.strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{EVENTS_FILENAME_PREFIX}{stamp}.json"
        payload = {
            "fetched_at": fetched_at.isoformat(),
            "events": [e.to_dict() for e in events],
        }
        path = self._cache_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def check_staleness(self) -> CacheInfo:
        path = self._find_latest_cache()
        if path is None:
            return CacheInfo(is_stale=False, age=timedelta(0))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            fetched_at = parse_iso8601_utc(data["fetched_at"])
        except (json.JSONDecodeError, KeyError, OSError, ValueError):
            return CacheInfo(is_stale=False, age=timedelta(0))
        age = datetime.now(timezone.utc) - fetched_at
        return CacheInfo(is_stale=age > timedelta(hours=CACHE_STALE_HOURS), age=age)

    def _find_latest_cache(self) -> pathlib.Path | None:
        if not self._cache_dir.is_dir():
            return None
        candidates = sorted(self._cache_dir.glob(EVENTS_CACHE_GLOB), reverse=True)
        if not candidates:
            return None
        return candidates[0]

    def _load_cache(self, path: pathlib.Path) -> list[Event]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [Event.from_dict(d) for d in data["events"]]
        except (json.JSONDecodeError, KeyError, OSError) as err:
            raise CacheError(f"Cannot read cache file {path}: {err}") from err


class MemoryProvider:
    """Holds events in memory.  Used by the eval runner."""

    def __init__(self, events: list[Event]) -> None:
        self._events = events

    def load(self) -> list[Event]:
        return self._events

    def save(self, events: list[Event], fetched_at: datetime) -> None:
        self._events = events

    def check_staleness(self) -> CacheInfo:
        return CacheInfo(is_stale=False, age=timedelta(0))


# ---------------------------------------------------------------------------
# EventStore
# ---------------------------------------------------------------------------

class EventStore:
    """Database-like abstraction over event storage.

    Provider binding is fixed after construction.  Callers interact only with
    ``query()``, ``save()``, and ``check_staleness()``.
    """

    def __init__(self, provider: EventProvider) -> None:
        self._provider = provider

    def query(
        self,
        params: QueryParams,
        *,
        seen_urls: set[str] | None = None,
    ) -> QueryResult:
        events = self._provider.load()
        return _filter_and_sort_events(events, params, seen_urls=seen_urls)

    def get_by_ids(self, ids: list[str]) -> list[Event]:
        events = self._provider.load()
        index = {e.id: e for e in events}
        return [index[eid] for eid in dict.fromkeys(ids) if eid in index]

    def save(self, events: list[Event], fetched_at: datetime) -> None:
        self._provider.save(events, fetched_at)

    def check_staleness(self) -> CacheInfo:
        return self._provider.check_staleness()


# ---------------------------------------------------------------------------
# Filter / sort engine (private)
# ---------------------------------------------------------------------------

def _filter_and_sort_events(
    events: list[Event],
    params: QueryParams,
    *,
    seen_urls: set[str] | None = None,
) -> QueryResult:
    """Filter, sort, and return events.  Pure function — no I/O."""

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

    if params.city and (params.search_lat is not None or params.search_lon is not None):
        raise QueryValidationError(
            "--city and coordinate search are mutually exclusive."
        )
    if (params.search_lat is None) != (params.search_lon is None):
        raise QueryValidationError(
            "Both search_lat and search_lon must be provided together."
        )
    if params.search_radius_miles is not None and params.search_lat is None:
        raise QueryValidationError(
            "search_radius_miles requires search_lat and search_lon."
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
        if start_utc <= parse_iso8601_utc(item.start_at) < end_utc
    ]
    if params.min_guest is not None:
        filtered = [
            item for item in filtered
            if item.guest_count >= params.min_guest
        ]
    if params.max_guest is not None:
        filtered = [
            item for item in filtered
            if item.guest_count <= params.max_guest
        ]
    if params.min_time is not None:
        filtered = [
            item for item in filtered
            if is_on_or_after_min_time(item.start_at, params.min_time)
        ]
    if params.max_time is not None:
        filtered = [
            item for item in filtered
            if parse_iso8601_utc(item.start_at).astimezone(la_tz).hour
            <= params.max_time
        ]
    if day_filter is not None:
        filtered = [
            item for item in filtered
            if parse_iso8601_utc(item.start_at).astimezone(la_tz).weekday()
            in day_filter
        ]
    if params.exclude:
        exclude_keywords = [
            k.strip().lower() for k in params.exclude.split(",") if k.strip()
        ]
        filtered = [
            item for item in filtered
            if not any(kw in item.title.lower() for kw in exclude_keywords)
        ]
    if params.search:
        search_term = params.search.lower()
        filtered = [
            item for item in filtered
            if search_term in item.title.lower()
        ]
    if regex_pattern is not None:
        filtered = [
            item for item in filtered
            if regex_pattern.search(item.title)
        ]
    if params.glob is not None:
        glob_pat = params.glob.lower()
        filtered = [
            item for item in filtered
            if fnmatch.fnmatch(item.title.lower(), glob_pat)
        ]

    if params.city is not None:
        city_lower = params.city.lower()
        filtered = [
            item for item in filtered
            if getattr(item, "city", None) is not None
            and item.city.lower() == city_lower
        ]
    if params.region is not None:
        region_lower = params.region.lower()
        filtered = [
            item for item in filtered
            if getattr(item, "region", None) is not None
            and item.region.lower() == region_lower
        ]
    if params.country is not None:
        country_lower = params.country.lower()
        filtered = [
            item for item in filtered
            if getattr(item, "country", None) is not None
            and item.country.lower() == country_lower
        ]
    if params.location_type is not None:
        lt_lower = params.location_type.lower()
        filtered = [
            item for item in filtered
            if getattr(item, "location_type", None) is not None
            and item.location_type.lower() == lt_lower
        ]
    if params.search_lat is not None and params.search_lon is not None:
        radius = params.search_radius_miles if params.search_radius_miles is not None else DEFAULT_SEARCH_RADIUS_MILES
        filtered = [
            item for item in filtered
            if item.latitude is not None and item.longitude is not None
            and _haversine_miles(params.search_lat, params.search_lon, item.latitude, item.longitude) <= radius
        ]

    # -- sort ----------------------------------------------------------------

    if params.sort == "date":
        filtered.sort(
            key=lambda x: (
                parse_iso8601_utc(x.start_at).astimezone(la_tz).date(),
                -x.guest_count,
                x.title.lower(),
            )
        )
    else:
        filtered.sort(
            key=lambda x: (
                -x.guest_count,
                parse_iso8601_utc(x.start_at),
                x.title.lower(),
            )
        )

    # -- seen-URL exclusion (after sort, matching original order) -------------

    if seen_urls is not None and not params.show_all:
        filtered = [
            item for item in filtered if item.url not in seen_urls
        ]

    return QueryResult(
        events=filtered,
        total_after_filter=len(filtered),
        window_start_utc=start_utc,
        window_end_utc=end_utc,
    )
