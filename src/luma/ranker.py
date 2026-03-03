"""Pure ranking module — calls LLM to rank candidate events by user preferences."""

from __future__ import annotations

import json
import os
import pathlib
import re

import anthropic

from luma.config import (
    AGENT_MAX_TOKENS,
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_AGENT_MODEL,
    SUGGEST_MAX_RESULTS,
)
from luma.models import Event

_PROMPT_PATH = pathlib.Path(__file__).parent / "ranker_prompt.md"
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class RankerError(Exception):
    """Raised for API failures and response parsing errors."""


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


def rank(
    liked: list[Event],
    disliked: list[Event],
    candidates: list[Event],
    *,
    max_results: int = SUGGEST_MAX_RESULTS,
) -> list[str]:
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not api_key:
        raise RankerError(
            f"Environment variable {ANTHROPIC_API_KEY_ENV} is not set."
        )

    template = _PROMPT_PATH.read_text(encoding="utf-8")
    prompt = template.format(
        liked_events=json.dumps(
            [_event_to_summary(e) for e in liked], indent=2
        ),
        disliked_events=json.dumps(
            [_event_to_summary(e) for e in disliked], indent=2
        )
        if disliked
        else "(none)",
        candidate_events=json.dumps(
            [_event_to_summary(e) for e in candidates], indent=2
        ),
        max_results=max_results,
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=DEFAULT_AGENT_MODEL,
            max_tokens=AGENT_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as exc:
        raise RankerError(f"Anthropic API error: {exc}") from exc

    text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    )

    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise RankerError(f"No JSON object in ranker response: {text[:500]}")

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise RankerError(f"Invalid JSON in ranker response: {exc}") from exc

    if not isinstance(data, dict) or "ids" not in data:
        raise RankerError(
            f"Ranker response missing 'ids' key: {text[:500]}"
        )

    raw_ids = data["ids"]
    if not isinstance(raw_ids, list):
        raise RankerError(f"'ids' is not a list: {type(raw_ids)}")

    candidate_ids = {e.id for e in candidates}
    valid_ids = [id_ for id_ in raw_ids if isinstance(id_, str) and id_ in candidate_ids]
    return valid_ids[:max_results]
