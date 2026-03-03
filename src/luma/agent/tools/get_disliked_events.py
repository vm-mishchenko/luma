"""GetDislikedEventsTool — retrieves user's disliked events."""

from __future__ import annotations

import json
from typing import Any

from luma.agent.tool import ToolResult
from luma.config import SUGGEST_MAX_DISLIKED
from luma.preference_store import PreferenceStore


class GetDislikedEventsTool:
    def __init__(self, preferences: PreferenceStore) -> None:
        self._preferences = preferences

    @property
    def name(self) -> str:
        return "get_disliked_events"

    @property
    def description(self) -> str:
        return f"Get events the user has disliked. Returns up to {SUGGEST_MAX_DISLIKED} most recent disliked events."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def loading_message(self) -> str:
        return "Loading disliked events"

    def execute(self, tool_input: dict[str, Any]) -> ToolResult:
        events = self._preferences.get_disliked()
        events.sort(key=lambda e: e.start_at, reverse=True)
        events = events[:SUGGEST_MAX_DISLIKED]
        return ToolResult(
            content=json.dumps([e.to_dict() for e in events]),
            is_error=False,
        )
