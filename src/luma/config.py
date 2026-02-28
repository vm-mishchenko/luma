"""Shared configuration values for luma modules."""

from __future__ import annotations

import pathlib

DEFAULT_CACHE_DIR = pathlib.Path.home() / ".cache" / "luma"

DEFAULT_WINDOW_DAYS = 14
CACHE_STALE_HOURS = 12
TIMEZONE_NAME = "America/Los_Angeles"

EVENTS_FILENAME_PREFIX = "events-"
EVENTS_CACHE_GLOB = f"{EVENTS_FILENAME_PREFIX}*.json"
SEEN_FILENAME = "seen.json"

API_BASE = "https://api2.luma.com"
FETCH_WINDOW_DAYS = 30
REQUEST_DELAY_SEC = 0.3
HARDCODED_LAT = "37.33939"
HARDCODED_LON = "-121.89496"
HARDCODED_CATEGORY_URLS = [
    "https://luma.com/ai",
    "https://luma.com/tech",
    "https://luma.com/sf",
]
HARDCODED_CALENDARS = [
    {"url": "https://luma.com/genai-sf", "calendar_api_id": "cal-JTdFQadEz0AOxyV"},
    {"url": "https://luma.com/frontiertower", "calendar_api_id": "cal-Sl7q1nHTRXQzjP2"},
    {"url": "https://luma.com/sf-hardware-meetup", "calendar_api_id": "cal-tFAzNGOZ9xn6kT2"},
    {"url": "https://luma.com/deepmind", "calendar_api_id": "cal-7Q5A70Bz5Idxopu"},
    {"url": "https://luma.com/genai-collective", "calendar_api_id": "cal-E74MDlDKBaeAwXK"},
    {"url": "https://luma.com/sfaiengineers", "calendar_api_id": "cal-EmYs2kgt1D9Gb27"},
    {"url": "https://luma.com/datadoghq", "calendar_api_id": "cal-58UTRXnfpeEA6ii"},
]
PAGINATION_LIMIT = "50"

DEFAULT_SORT = "date"
DEFAULT_SEARCH_RADIUS_MILES = 5
DEFAULT_RETRIES = 5

ANTHROPIC_API_KEY_ENV = "LUMA_ANTHROPIC_API_KEY"
DEFAULT_AGENT_MODEL = "claude-sonnet-4-20250514"
# DEFAULT_AGENT_MODEL = "claude-opus-4-20250514"
DEFAULT_AGENT_MAX_ITERATIONS = 5
AGENT_MAX_TOKENS = 4096
AGENT_MAX_PARALLEL_TOOLS = 10
AGENT_LLM_TIMEOUT_SECONDS = 10
AGENT_TOOL_TIMEOUT_SECONDS = 10


def _reset() -> None:
    """No-op kept for e2e test fixture compatibility."""
