"""Persistence layer for liked/disliked event preferences.

Providers handle storage mechanics (disk or memory).  Callers construct a
provider, pass it to ``PreferenceStore``, and interact only with the store.
"""

from __future__ import annotations

import json
import pathlib
from typing import Protocol

from luma.config import DISLIKED_FILENAME, LIKED_FILENAME
from luma.models import Event


# ---------------------------------------------------------------------------
# Provider protocol & implementations
# ---------------------------------------------------------------------------

class PreferenceProvider(Protocol):
    def load_liked(self) -> list[Event]: ...
    def load_disliked(self) -> list[Event]: ...
    def save_liked(self, events: list[Event]) -> None: ...
    def save_disliked(self, events: list[Event]) -> None: ...


class DiskPreferenceProvider:
    """Reads and writes preference files on disk under *preferences_dir*."""

    def __init__(self, preferences_dir: pathlib.Path) -> None:
        self._preferences_dir = preferences_dir

    def load_liked(self) -> list[Event]:
        return self._load(LIKED_FILENAME)

    def load_disliked(self) -> list[Event]:
        return self._load(DISLIKED_FILENAME)

    def save_liked(self, events: list[Event]) -> None:
        self._save(events, LIKED_FILENAME)

    def save_disliked(self, events: list[Event]) -> None:
        self._save(events, DISLIKED_FILENAME)

    def _load(self, filename: str) -> list[Event]:
        path = self._preferences_dir / filename
        if not path.is_file():
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [Event.model_validate(d) for d in data]
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            pass
        return []

    def _save(self, events: list[Event], filename: str) -> None:
        self._preferences_dir.mkdir(parents=True, exist_ok=True)
        path = self._preferences_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump([e.model_dump() for e in events], f, indent=2)


class MemoryPreferenceProvider:
    """Holds preferences in memory.  Used by the eval runner."""

    def __init__(
        self,
        liked: list[Event] | None = None,
        disliked: list[Event] | None = None,
    ) -> None:
        self._liked = liked or []
        self._disliked = disliked or []

    def load_liked(self) -> list[Event]:
        return self._liked

    def load_disliked(self) -> list[Event]:
        return self._disliked

    def save_liked(self, events: list[Event]) -> None:
        self._liked = events

    def save_disliked(self, events: list[Event]) -> None:
        self._disliked = events


# ---------------------------------------------------------------------------
# PreferenceStore
# ---------------------------------------------------------------------------

class PreferenceStore:
    """High-level abstraction over preference storage.

    Provider binding is fixed after construction.  Contains the "last action
    wins" dedup logic: adding to liked removes from disliked, and vice versa.
    """

    def __init__(self, provider: PreferenceProvider) -> None:
        self._provider = provider

    def get_liked(self) -> list[Event]:
        return self._provider.load_liked()

    def get_disliked(self) -> list[Event]:
        return self._provider.load_disliked()

    def get_liked_ids(self) -> set[str]:
        return {e.id for e in self._provider.load_liked()}

    def get_disliked_ids(self) -> set[str]:
        return {e.id for e in self._provider.load_disliked()}

    def add_liked(self, events: list[Event]) -> int:
        return self._add(events, like=True)

    def add_disliked(self, events: list[Event]) -> int:
        return self._add(events, like=False)

    def _add(self, events: list[Event], *, like: bool) -> int:
        if like:
            target = self._provider.load_liked()
            opposite = self._provider.load_disliked()
        else:
            target = self._provider.load_disliked()
            opposite = self._provider.load_liked()

        existing_ids = {e.id for e in target}
        new_events = [e for e in events if e.id not in existing_ids]
        if not new_events:
            return 0

        target.extend(new_events)

        new_ids = {e.id for e in new_events}
        opposite = [e for e in opposite if e.id not in new_ids]

        if like:
            self._provider.save_liked(target)
            self._provider.save_disliked(opposite)
        else:
            self._provider.save_disliked(target)
            self._provider.save_liked(opposite)

        return len(new_events)
