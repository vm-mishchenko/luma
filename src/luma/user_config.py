"""User configuration — TOML loading, validation, and template auto-creation."""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib
from dataclasses import dataclass


CONFIG_TEMPLATE = """\
[llm]
provider = "anthropic"

[llm.anthropic]
api_key = "sk-ant-..."
model = "claude-sonnet-4-20250514"

# [llm.ollama]
# host = "http://localhost:11434"
# model = "llama3.1"
# timeout = 120

# [storage]
# provider = "mongo"
# [storage.mongo]
# connection_string = "mongodb://localhost:27017"
# database = "luma"

# Shortcuts: named queries callable via 'luma sc <name>'
# Each shortcut is an array of CLI arguments.
# Example:
# [shortcuts]
# popular = ["--sort", "guest", "--min-guest", "100"]
# tomorrow = ["--range", "tomorrow"]
# weekend = ["--range", "weekend"]
"""

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


def get_llm_config(config: dict, provider_override: str | None = None) -> LLMConfig:
    """Build LLMConfig from [llm] section in config."""
    llm = config.get("llm")
    if not llm or not isinstance(llm, dict):
        print("Error: Add [llm] section to config.toml.", file=sys.stderr)
        raise SystemExit(2)

    provider_name = provider_override or llm.get("provider")
    if not provider_name:
        print("Error: Set provider in [llm] section of config.toml.", file=sys.stderr)
        raise SystemExit(2)

    provider_block = llm.get(provider_name)
    if not provider_block or not isinstance(provider_block, dict):
        print(
            f"Error: [llm.{provider_name}] section not found in config.toml.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    model = provider_block.get("model")
    if not model:
        print(
            f"Error: Set model in [llm.{provider_name}] section of config.toml.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    api_key = provider_block.get("api_key")
    if provider_name == "anthropic" and not api_key:
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
