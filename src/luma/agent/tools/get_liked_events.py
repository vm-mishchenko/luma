"""GetLikedEventsTool — retrieves user's liked events."""

from __future__ import annotations

import json
from typing import Any

from luma.agent.tool import ToolResult
from luma.config import SUGGEST_MAX_LIKED
from luma.preference_store import PreferenceStore


class GetLikedEventsTool:
    def __init__(self, preferences: PreferenceStore) -> None:
        self._preferences = preferences

    @property
    def name(self) -> str:
        return "get_liked_events"

    @property
    def description(self) -> str:
        return f"Get events the user has liked. Returns up to {SUGGEST_MAX_LIKED} most recent liked events."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def loading_message(self) -> str:
        return "Loading liked events"

    def execute(self, tool_input: dict[str, Any]) -> ToolResult:
        events = self._preferences.get_liked()
        events.sort(key=lambda e: e.start_at, reverse=True)
        events = events[:SUGGEST_MAX_LIKED]
        return ToolResult(
            content=json.dumps([e.to_dict() for e in events]),
            is_error=False,
        )
