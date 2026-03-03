"""Suggest command — LLM-powered event recommendations based on preferences."""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

from pydantic import BaseModel, ValidationError

from luma.agent import Agent, AgentError, AgentResult, EventListResult, FinalResult
from luma.command_query import _Loader, _print_events
from luma.config import (
    SUGGEST_MAX_DISLIKED,
    SUGGEST_MAX_LIKED,
    SUGGEST_MAX_RESULTS,
)
from luma.event_store import CacheError, EventStore, QueryParams
from luma.models import Event
from luma.preference_store import PreferenceStore

_DIM = "\033[2m"
_RESET = "\033[0m"

_RANKER_PROMPT_PATH = pathlib.Path(__file__).parent / "agent" / "prompts" / "ranker.md"


class _RankerResponse(BaseModel):
    ids: list[str]


def _parse_ranker_response(data: Any) -> AgentResult:
    try:
        validated = _RankerResponse.model_validate(data)
    except ValidationError as exc:
        raise AgentError(
            f"Ranker response does not match schema: {exc}"
        ) from exc
    return EventListResult(ids=validated.ids)


def _is_tty() -> bool:
    return sys.stderr.isatty()


def _status(msg: str) -> None:
    if _is_tty():
        print(f"{_DIM}{msg}{_RESET}", file=sys.stderr)
    else:
        print(msg, file=sys.stderr)


def _event_to_summary(event: Event) -> dict:
    return {
        "id": event.id,
        "title": event.title,
        "start_at": event.start_at,
        "guest_count": event.guest_count,
        "city": event.city,
        "location_type": event.location_type,
        "hosts": [h.name for h in event.hosts],
    }


def _build_ranker_message(
    liked: list[Event],
    disliked: list[Event],
    candidates: list[Event],
    max_results: int,
) -> str:
    liked_json = json.dumps(
        [_event_to_summary(e) for e in liked], indent=2
    )
    disliked_json = (
        json.dumps([_event_to_summary(e) for e in disliked], indent=2)
        if disliked
        else "(none)"
    )
    candidates_json = json.dumps(
        [_event_to_summary(e) for e in candidates], indent=2
    )
    return (
        f"## Liked events\n{liked_json}\n\n"
        f"## Disliked events\n{disliked_json}\n\n"
        f"## Candidate events to rank\n{candidates_json}\n\n"
        f'Return a JSON object with an "ids" key containing up to {max_results} '
        f"event IDs from the candidate list, ordered from most to least relevant.\n"
        f"Only return IDs that appear in the candidate events. Do not invent IDs.\n\n"
        f'Response format:\n{{"ids": ["evt-123", "evt-456"]}}'
    )


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

    max_results = top if top is not None else SUGGEST_MAX_RESULTS

    system_prompt = _RANKER_PROMPT_PATH.read_text(encoding="utf-8")
    user_message = _build_ranker_message(liked, disliked, candidates, max_results)

    agent = Agent(
        store=store,
        preferences=preferences,
        system_prompt=system_prompt,
        tools=None,
        expected_output=_parse_ranker_response,
    )

    loader = _Loader()
    try:
        for item in agent.query_iter(user_message, loader=loader):
            if isinstance(item, FinalResult):
                result = item.result
                if isinstance(result, EventListResult):
                    candidate_ids = {e.id for e in candidates}
                    valid_ids = [
                        id_ for id_ in result.ids
                        if isinstance(id_, str) and id_ in candidate_ids
                    ]
                    ranked_ids = valid_ids[:max_results]

                    candidate_map = {e.id: e for e in candidates}
                    ranked_events = [
                        candidate_map[id_] for id_ in ranked_ids if id_ in candidate_map
                    ]

                    if not ranked_events:
                        print("No suggestions found.", file=sys.stderr)
                        return 0

                    _print_events(ranked_events, sort="date")
                    return 0
    except AgentError as e:
        loader.stop()
        print(f"Ranking error: {e}", file=sys.stderr)
        return 1

    print("No suggestions found.", file=sys.stderr)
    return 0
