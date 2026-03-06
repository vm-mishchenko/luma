"""GetEventDetailTool — fetches full details for a single event."""

from __future__ import annotations

import json
from typing import Any

from luma.agent.tool import ToolResult
from luma.download import fetch_event_detail
from luma.models import EventDetail


class GetEventDetailTool:
    @property
    def name(self) -> str:
        return "get_event_detail"

    @property
    def description(self) -> str:
        props = EventDetail.model_json_schema()["properties"]
        return (
            "Fetch full details for a single event by its ID. "
            "Use when the user asks what an event is about, its description, topics, or categories. "
            "Returns: " + json.dumps(props)
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "The event ID (starts with evt-)",
                }
            },
            "required": ["event_id"],
        }

    @property
    def loading_message(self) -> str:
        return "Fetching event details"

    def execute(self, tool_input: dict[str, Any]) -> ToolResult:
        event_id = tool_input.get("event_id", "")
        if not event_id:
            return ToolResult(content="Tool error: event_id is required", is_error=True)
        try:
            detail = fetch_event_detail(event_id)
            return ToolResult(
                content=json.dumps(detail.model_dump()),
                is_error=False,
            )
        except Exception as exc:
            return ToolResult(content=f"Tool error: {exc}", is_error=True)
