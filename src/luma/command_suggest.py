"""Suggest command — LLM-powered event recommendations based on preferences."""

from __future__ import annotations

import sys
import threading
import time

import luma.ranker as ranker
from luma.command_query import _print_events
from luma.config import (
    SUGGEST_MAX_DISLIKED,
    SUGGEST_MAX_LIKED,
)
from luma.event_store import CacheError, EventStore, QueryParams
from luma.preference_store import PreferenceStore
from luma.ranker import RankerError

_DIM = "\033[2m"
_RESET = "\033[0m"
_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def _is_tty() -> bool:
    return sys.stderr.isatty()


def _status(msg: str) -> None:
    if _is_tty():
        print(f"{_DIM}{msg}{_RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


class _Spinner:
    """Animated spinner on stderr, no-op when not a TTY."""

    def __init__(self, label: str) -> None:
        self._label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Spinner:
        if not _is_tty():
            print(f"{self._label}...", file=sys.stderr)
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        print("\r\033[K", end="", file=sys.stderr, flush=True)

    def _run(self) -> None:
        idx = 0
        while not self._stop.is_set():
            frame = _FRAMES[idx % len(_FRAMES)]
            print(f"\r{frame} {self._label} ", end="", file=sys.stderr, flush=True)
            idx += 1
            time.sleep(0.08)


def run(store: EventStore, preferences: PreferenceStore, *, top: int | None = None) -> int:
    has_cache = True
    try:
        result = store.query(QueryParams())
        candidates = result.events
        _status(f"Loaded {len(candidates)} cached events (next 14 days).")
    except CacheError:
        has_cache = False
        candidates = []

    liked = preferences.get_liked()
    disliked = preferences.get_disliked()
    has_liked = len(liked) > 0
    _status(f"Preferences: {len(liked)} liked, {len(disliked)} disliked.")

    if not has_cache and not has_liked:
        print(
            "No cached events and no liked events.\n"
            "Run 'luma refresh' to fetch events, then 'luma like' to mark favorites.",
            file=sys.stderr,
        )
        return 1
    if not has_cache:
        print("No cached events. Run 'luma refresh' first.", file=sys.stderr)
        return 1
    if not has_liked:
        print(
            "No liked events. Like some events first:\n"
            "  luma like\n"
            '  luma like --search "AI"',
            file=sys.stderr,
        )
        return 1

    liked.sort(key=lambda e: e.start_at, reverse=True)
    liked = liked[:SUGGEST_MAX_LIKED]
    disliked.sort(key=lambda e: e.start_at, reverse=True)
    disliked = disliked[:SUGGEST_MAX_DISLIKED]

    from luma.config import SUGGEST_MAX_RESULTS

    max_results = top if top is not None else SUGGEST_MAX_RESULTS
    try:
        with _Spinner("Ranking events"):
            ranked_ids = ranker.rank(liked, disliked, candidates, max_results=max_results)
    except RankerError as e:
        print(f"Ranking error: {e}", file=sys.stderr)
        return 1

    candidate_map = {e.id: e for e in candidates}
    ranked_events = [candidate_map[id_] for id_ in ranked_ids if id_ in candidate_map]

    if not ranked_events:
        print("No suggestions found.", file=sys.stderr)
        return 0

    _print_events(ranked_events, sort="date")
    return 0
