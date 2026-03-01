"""User configuration — TOML loading, validation, and template auto-creation."""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib


CONFIG_TEMPLATE = """\
# Anthropic API key (fallback if LUMA_ANTHROPIC_API_KEY env var is not set)
# api_key = "sk-ant-..."

# Shortcuts: named queries callable via 'luma sc <name>'
# Each shortcut is an array of CLI arguments.
# Example:
# [shortcuts]
# popular = ["--sort", "guest", "--min-guest", "100"]
# tomorrow = ["--range", "tomorrow"]
# weekend = ["--range", "weekend"]
"""

_SHORTCUT_NAME_RE = re.compile(r"^[a-zA-Z0-9-]+$")


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
    """Validate shortcuts structure in the parsed config."""
    shortcuts = config.get("shortcuts")
    if shortcuts is None:
        return
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


def get_shortcuts(config: dict) -> dict[str, list[str]]:
    """Return the shortcuts mapping from config."""
    return config.get("shortcuts", {})


def get_api_key(config: dict) -> str | None:
    """Return the api_key value from config, if present."""
    return config.get("api_key")
