"""QueryEventsTool — searches and filters events from the store."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from luma.agent.agent import AgentQueryParams, _to_query_params
from luma.agent.tool import ToolResult
from luma.event_store import CacheError, EventStore, QueryValidationError
from luma.models import Event


class QueryEventsTool:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "query_events"

    @property
    def description(self) -> str:
        props = Event.model_json_schema()["properties"]
        return (
            "Search and filter events from the database. "
            "Returns matching events sorted by the specified criteria. "
            "When you need multiple independent queries (e.g. compare different date ranges), "
            "include all tool calls in one response. "
            "Returns: " + json.dumps(props)
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        schema = AgentQueryParams.model_json_schema()
        schema.pop("title", None)
        return schema

    @property
    def loading_message(self) -> str:
        return "Searching events"

    def execute(self, tool_input: dict[str, Any]) -> ToolResult:
        try:
            agent_params = AgentQueryParams.model_validate(tool_input)
            params = _to_query_params(agent_params)
            result = self._store.query(params)
            return ToolResult(
                content=json.dumps([e.model_dump() for e in result.events]),
                is_error=False,
                metadata={"fetch": True},
            )
        except (ValidationError, QueryValidationError, CacheError) as exc:
            return ToolResult(content=f"Tool error: {exc}", is_error=True)
