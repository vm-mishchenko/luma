"""User configuration — TOML loading, validation, and template auto-creation."""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib
from dataclasses import dataclass


DEFAULT_LATITUDE = 37.33939
DEFAULT_LONGITUDE = -121.89496

DEFAULT_REFRESH_CATEGORIES: tuple[str, ...] = (
    "https://luma.com/ai",
    "https://luma.com/tech",
    "https://luma.com/sf",
)

DEFAULT_REFRESH_CALENDARS: tuple[dict[str, str], ...] = (
    {"url": "https://luma.com/genai-sf", "calendar_api_id": "cal-JTdFQadEz0AOxyV"},
    {"url": "https://luma.com/frontiertower", "calendar_api_id": "cal-Sl7q1nHTRXQzjP2"},
    {"url": "https://luma.com/sf-hardware-meetup", "calendar_api_id": "cal-tFAzNGOZ9xn6kT2"},
    {"url": "https://luma.com/deepmind", "calendar_api_id": "cal-7Q5A70Bz5Idxopu"},
    {"url": "https://luma.com/genai-collective", "calendar_api_id": "cal-E74MDlDKBaeAwXK"},
    {"url": "https://luma.com/sfaiengineers", "calendar_api_id": "cal-EmYs2kgt1D9Gb27"},
    {"url": "https://luma.com/datadoghq", "calendar_api_id": "cal-58UTRXnfpeEA6ii"},
)


def _format_default_refresh_toml() -> str:
    lines = ["[refresh]", "categories = ["]
    for url in DEFAULT_REFRESH_CATEGORIES:
        lines.append(f'  "{url}",')
    lines.append("]")
    lines.append("")
    for cal in DEFAULT_REFRESH_CALENDARS:
        lines.append("[[refresh.calendars]]")
        lines.append(f'url = "{cal["url"]}"')
        lines.append(f'calendar_api_id = "{cal["calendar_api_id"]}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


CONFIG_TEMPLATE = """\
# LLM provider for free-text queries and suggestions.
# Requires an API key from the chosen provider.
# [llm]
# provider = "anthropic"

# [llm.anthropic]
# api_key = ""  # Get your key at https://console.anthropic.com/
# model = "claude-sonnet-4-20250514"

# [llm.ollama]
# host = "http://localhost:11434"
# model = "llama3.1"
# timeout = 120

# Event storage backend. Default: local disk cache.
# [storage]
# provider = "mongo"
# [storage.mongo]
# connection_string = "mongodb://localhost:27017"
# database = "luma"

# Named queries callable via 'luma sc <name>'.
# Each shortcut is an array of CLI arguments.
# [shortcuts]
# popular = ["--sort", "guest", "--min-guest", "100"]
# tomorrow = ["--range", "tomorrow"]
# weekend = ["--range", "weekend"]

# Coordinates used for category discovery during 'luma refresh'.
# Default: San Jose, CA. Change to your city to get locally relevant events.
[location]
latitude = """ + str(DEFAULT_LATITUDE) + """
longitude = """ + str(DEFAULT_LONGITUDE) + """

# Event sources to fetch during 'luma refresh'.
# Categories are Luma discover pages; calendars are specific organizer pages.
""" + _format_default_refresh_toml()

_SHORTCUT_NAME_RE = re.compile(r"^[a-zA-Z0-9-]+$")


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str | None = None
    api_base: str | None = None
    timeout: int | None = None
    reasoning_effort: str | None = None


def ensure_config(path: pathlib.Path) -> None:
    """Create config file with template if it does not exist."""
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")


def load_config(path: pathlib.Path) -> dict:
    """Read and parse a TOML config file."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        print(f"Error: malformed config file {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def validate_config(config: dict) -> None:
    """Validate shortcuts and [llm] structure in the parsed config."""
    shortcuts = config.get("shortcuts")
    if shortcuts is not None:
        if not isinstance(shortcuts, dict):
            print("Error: [shortcuts] must be a table.", file=sys.stderr)
            raise SystemExit(2)
        for name, value in shortcuts.items():
            if not _SHORTCUT_NAME_RE.match(name):
                print(
                    f"Error: shortcut name '{name}' is invalid. "
                    "Use only letters, digits, and hyphens.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                print(
                    f"Error: shortcut '{name}' must be an array of strings.",
                    file=sys.stderr,
                )
                raise SystemExit(2)

    llm = config.get("llm")
    if llm is not None:
        if not isinstance(llm, dict):
            print("Error: [llm] must be a table.", file=sys.stderr)
            raise SystemExit(2)
        provider = llm.get("provider")
        if provider is not None and not isinstance(provider, str):
            print("Error: [llm].provider must be a string.", file=sys.stderr)
            raise SystemExit(2)
        for key, val in llm.items():
            if key == "provider":
                continue
            if isinstance(val, dict):
                continue

    storage = config.get("storage")
    if storage is not None:
        if not isinstance(storage, dict):
            print("Error: [storage] must be a table.", file=sys.stderr)
            raise SystemExit(2)
        sp = storage.get("provider")
        if sp is not None and not isinstance(sp, str):
            print("Error: [storage].provider must be a string.", file=sys.stderr)
            raise SystemExit(2)
        if sp == "mongo":
            mongo = storage.get("mongo")
            if not mongo or not isinstance(mongo, dict):
                print("Error: [storage.mongo] section required when provider = \"mongo\".", file=sys.stderr)
                raise SystemExit(2)
            if not isinstance(mongo.get("connection_string"), str):
                print("Error: Set connection_string in [storage.mongo].", file=sys.stderr)
                raise SystemExit(2)
            if not isinstance(mongo.get("database"), str):
                print("Error: Set database in [storage.mongo].", file=sys.stderr)
                raise SystemExit(2)

    location = config.get("location")
    if location is not None:
        if not isinstance(location, dict):
            print("Error: [location] must be a table.", file=sys.stderr)
            raise SystemExit(2)
        for field, lo, hi in (
            ("latitude", -90, 90),
            ("longitude", -180, 180),
        ):
            if field not in location:
                print(f"Error: [location].{field} is required.", file=sys.stderr)
                raise SystemExit(2)
            val = location[field]
            if not isinstance(val, (int, float)):
                print(
                    f"Error: [location].{field} must be a number, got {val!r}.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            if not (lo <= val <= hi):
                print(
                    f"Error: [location].{field} = {val} is out of range ({lo} to {hi}).",
                    file=sys.stderr,
                )
                raise SystemExit(2)

    refresh = config.get("refresh")
    if refresh is not None:
        if not isinstance(refresh, dict):
            print("Error: [refresh] must be a table.", file=sys.stderr)
            raise SystemExit(2)
        if "categories" in refresh:
            cats = refresh["categories"]
            if not isinstance(cats, list) or not all(isinstance(x, str) for x in cats):
                print(
                    "Error: [refresh].categories must be an array of strings.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
        if "calendars" in refresh:
            cals = refresh["calendars"]
            if not isinstance(cals, list):
                print("Error: [refresh].calendars must be an array.", file=sys.stderr)
                raise SystemExit(2)
            for i, row in enumerate(cals):
                if not isinstance(row, dict):
                    print(
                        f"Error: [refresh].calendars[{i}] must be a table.",
                        file=sys.stderr,
                    )
                    raise SystemExit(2)
                if not isinstance(row.get("url"), str):
                    print(
                        f"Error: [refresh].calendars[{i}] must have a string url.",
                        file=sys.stderr,
                    )
                    raise SystemExit(2)
                if "calendar_api_id" in row:
                    cid = row["calendar_api_id"]
                    if cid is not None and not isinstance(cid, str):
                        print(
                            f"Error: [refresh].calendars[{i}].calendar_api_id must be a string.",
                            file=sys.stderr,
                        )
                        raise SystemExit(2)


def _dedupe_category_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _dedupe_calendars(
    rows: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    seen: set[str] = set()
    out: list[dict[str, str | None]] = []
    for row in rows:
        url = row["url"]
        if url in seen:
            continue
        seen.add(url)
        cid = row.get("calendar_api_id")
        out.append({"url": url, "calendar_api_id": cid})
    return out


def get_refresh_sources(config: dict) -> tuple[list[str], list[dict[str, str | None]]]:
    """Resolve category URLs and calendar rows from config only (dedupe). No runtime defaults."""
    refresh_tbl = config.get("refresh")
    if refresh_tbl is None:
        categories: list[str] = []
        calendars: list[dict[str, str | None]] = []
    else:
        categories = list(refresh_tbl.get("categories", []))
        calendars_raw = refresh_tbl.get("calendars", [])
        calendars = []
        for row in calendars_raw:
            r = row  # validated dict
            cid = r.get("calendar_api_id")
            calendars.append({"url": r["url"], "calendar_api_id": cid})

    categories = _dedupe_category_urls(categories)
    calendars = _dedupe_calendars(calendars)
    return categories, calendars


def get_location(config: dict) -> tuple[str, str]:
    """Return (latitude, longitude) as strings from [location] or defaults."""
    location = config.get("location")
    if location is None:
        return str(DEFAULT_LATITUDE), str(DEFAULT_LONGITUDE)
    return str(location["latitude"]), str(location["longitude"])


def get_llm_config(
    config: dict,
    provider_override: str | None = None,
    *,
    required: bool = True,
) -> LLMConfig | None:
    """Build LLMConfig from [llm] section in config.

    When *required* is False, returns None instead of exiting on missing config.
    """
    llm = config.get("llm")
    if not llm or not isinstance(llm, dict):
        if not required:
            return None
        print("Error: Add [llm] section to config.toml.", file=sys.stderr)
        raise SystemExit(2)

    provider_name = provider_override or llm.get("provider")
    if not provider_name:
        if not required:
            return None
        print("Error: Set provider in [llm] section of config.toml.", file=sys.stderr)
        raise SystemExit(2)

    provider_block = llm.get(provider_name)
    if not provider_block or not isinstance(provider_block, dict):
        if not required:
            return None
        print(
            f"Error: [llm.{provider_name}] section not found in config.toml.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    model = provider_block.get("model")
    if not model:
        if not required:
            return None
        print(
            f"Error: Set model in [llm.{provider_name}] section of config.toml.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    api_key = provider_block.get("api_key")
    if provider_name == "anthropic" and not api_key:
        if not required:
            return None
        print(
            "Error: Set api_key in [llm.anthropic] section of config.toml.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    api_base = provider_block.get("host")
    timeout = provider_block.get("timeout")
    reasoning_effort = provider_block.get("reasoning_effort")

    return LLMConfig(
        provider=provider_name,
        model=model,
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
    )


def get_event_provider(config: dict, cache_dir: pathlib.Path):
    """Build an EventProvider from [storage] section in config."""
    from luma.event_store import DiskProvider

    storage = config.get("storage")
    if not storage or storage.get("provider", "disk") == "disk":
        return DiskProvider(cache_dir)

    if storage["provider"] == "mongo":
        from pymongo import MongoClient

        from luma.mongo_provider import MongoEventProvider

        mongo = storage["mongo"]
        client = MongoClient(mongo["connection_string"])
        return MongoEventProvider(client[mongo["database"]])

    print(f"Error: Unknown storage provider '{storage['provider']}'.", file=sys.stderr)
    raise SystemExit(2)


def get_shortcuts(config: dict) -> dict[str, list[str]]:
    """Return the shortcuts mapping from config."""
    return config.get("shortcuts", {})
