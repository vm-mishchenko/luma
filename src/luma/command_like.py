"""Like command — interactive preference selection with inline dislike support.

Users enter bare numbers to like events and minus-prefixed numbers to dislike:
    Like/dislike (e.g. 1 3 -2): 1 3 -2
"""

from __future__ import annotations

import argparse
import sys

from luma.command_query import _build_query_params, _format_los_angeles_time
from luma.event_store import CacheError, EventStore, QueryValidationError
from luma.preference_store import PreferenceStore


def run(
    args: argparse.Namespace,
    store: EventStore,
    preferences: PreferenceStore,
) -> int:
    if not sys.stdin.isatty():
        print("like requires an interactive terminal.", file=sys.stderr)
        return 2

    params = _build_query_params(args)
    try:
        result = store.query(params)
    except CacheError as e:
        print(str(e), file=sys.stderr)
        return 1
    except QueryValidationError as e:
        print(str(e), file=sys.stderr)
        return 2

    events = result.events[: args.top] if args.top else result.events

    liked_ids = preferences.get_liked_ids()
    disliked_ids = preferences.get_disliked_ids()
    already_rated = liked_ids | disliked_ids
    events = [e for e in events if e.id not in already_rated]

    if not events:
        print("All events already rated.", file=sys.stderr)
        return 0

    score_width = max(len(f"[{e.guest_count}]") for e in events)
    date_width = max(len(_format_los_angeles_time(e.start_at)) for e in events)
    for i, e in enumerate(events, 1):
        score_text = f"[{e.guest_count}]".ljust(score_width)
        date_text = _format_los_angeles_time(e.start_at).ljust(date_width)
        print(f"  {i}. {score_text} {date_text} | {e.title} | {e.url}")

    try:
        raw = input("Like/dislike (e.g. 1 3 -2): ")
    except (KeyboardInterrupt, EOFError):
        return 0

    raw = raw.strip()
    if not raw:
        return 0

    tokens = raw.replace(",", " ").split()
    like_indices: list[int] = []
    dislike_indices: list[int] = []
    for tok in tokens:
        try:
            val = int(tok)
        except ValueError:
            print(f"Invalid input: '{tok}' is not a number.", file=sys.stderr)
            return 2
        if val == 0:
            print("Invalid number: 0. Use positive to like, negative to dislike.", file=sys.stderr)
            return 2
        idx = abs(val)
        if idx < 1 or idx > len(events):
            print(
                f"Invalid number: {val}. Must be between 1 and {len(events)} (or -{len(events)} to -1).",
                file=sys.stderr,
            )
            return 2
        if val > 0:
            like_indices.append(idx)
        else:
            dislike_indices.append(idx)

    if not like_indices and not dislike_indices:
        return 0

    liked_count = 0
    disliked_count = 0
    if like_indices:
        selected = [events[i - 1] for i in like_indices]
        liked_count = preferences.add_liked(selected)
    if dislike_indices:
        selected = [events[i - 1] for i in dislike_indices]
        disliked_count = preferences.add_disliked(selected)

    parts = []
    if liked_count:
        parts.append(f"Liked {liked_count}")
    if disliked_count:
        parts.append(f"disliked {disliked_count}")
    if parts:
        print(f"{', '.join(parts)}.", file=sys.stderr)
    return 0
