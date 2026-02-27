"""LLM-powered agent with tool-calling loop."""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Union

import anthropic
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from zoneinfo import ZoneInfo

from luma.config import (
    AGENT_MAX_TOKENS,
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_AGENT_MAX_ITERATIONS,
    DEFAULT_AGENT_MODEL,
    TIMEZONE_NAME,
)
from luma.event_store import CacheError, EventStore, QueryParams, QueryValidationError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AgentError(Exception):
    """Raised for any agent failure."""


# ---------------------------------------------------------------------------
# Public result types (unchanged API)
# ---------------------------------------------------------------------------

@dataclass
class EventListResult:
    events: list[dict[str, Any]]


@dataclass
class TextResult:
    text: str


AgentResult = EventListResult | TextResult


# ---------------------------------------------------------------------------
# Pydantic models for LLM response parsing
# ---------------------------------------------------------------------------

class TextResponse(BaseModel):
    type: Literal["text"]
    text: str


class EventsResponse(BaseModel):
    type: Literal["events"]
    events: list[dict[str, Any]]


RESPONSE_ADAPTER: TypeAdapter[TextResponse | EventsResponse] = TypeAdapter(
    Annotated[
        Union[TextResponse, EventsResponse],
        Field(discriminator="type"),
    ]
)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

QUERY_EVENTS_TOOL: dict[str, Any] = {
    "name": "query_events",
    "description": (
        "Search and filter events from the database. "
        "Returns matching events sorted by the specified criteria."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of days from today to include. Mutually exclusive with from_date/to_date.",
            },
            "from_date": {
                "type": "string",
                "description": "Start date in YYYYMMDD format (inclusive). Mutually exclusive with days.",
            },
            "to_date": {
                "type": "string",
                "description": "End date in YYYYMMDD format (inclusive). Mutually exclusive with days.",
            },
            "min_guest": {
                "type": "integer",
                "description": "Minimum guest count to include (default: 50).",
            },
            "max_guest": {
                "type": "integer",
                "description": "Maximum guest count to include.",
            },
            "min_time": {
                "type": "integer",
                "description": "Minimum event start hour in Los Angeles time (0-23).",
            },
            "max_time": {
                "type": "integer",
                "description": "Maximum event start hour in Los Angeles time (0-23).",
            },
            "day": {
                "type": "string",
                "description": "Comma-separated weekday filter, e.g. 'Sat,Sun'. Case-insensitive.",
            },
            "exclude": {
                "type": "string",
                "description": "Comma-separated keywords to exclude from titles (case-insensitive).",
            },
            "search": {
                "type": "string",
                "description": "Keyword search in event titles (case-insensitive). Mutually exclusive with regex and glob.",
            },
            "regex": {
                "type": "string",
                "description": "Regex pattern to match event titles (case-insensitive). Mutually exclusive with search and glob.",
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to match event titles (case-insensitive, e.g. '*AI*meetup*'). Mutually exclusive with search and regex.",
            },
            "sort": {
                "type": "string",
                "enum": ["date", "guest"],
                "description": "Sort by event date (default) or guest count.",
            },
        },
        "required": [],
    },
}

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_MARKDOWN_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """LLM-powered agent that queries events."""

    RESPONSE = "I'm Luma assistant. I can help you find events."

    def __init__(
        self,
        store: EventStore,
        *,
        model: str = DEFAULT_AGENT_MODEL,
        max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
    ) -> None:
        self._store = store
        self._model = model
        self._max_iterations = max_iterations

    def run(self, messages: list[dict[str, str]]) -> Iterator[str]:
        _ = messages
        for token in self.RESPONSE.split():
            yield token

    def query(self, text: str, params: QueryParams) -> AgentResult:
        api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
        if not api_key:
            raise AgentError(
                f"Environment variable {ANTHROPIC_API_KEY_ENV} is not set. "
                "Set it to your Anthropic API key to use the agent."
            )

        client = anthropic.Anthropic(api_key=api_key)
        system_prompt = self._build_system_prompt()
        user_message = self._build_user_message(text, params)
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        for _ in range(self._max_iterations):
            try:
                response = client.messages.create(
                    model=self._model,
                    max_tokens=AGENT_MAX_TOKENS,
                    system=system_prompt,
                    messages=messages,
                    tools=[QUERY_EVENTS_TOOL],
                )
            except anthropic.APIError as exc:
                raise AgentError(f"Anthropic API error: {exc}") from exc

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result_content, is_error = self._execute_tool(
                            block.name, block.input
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_content,
                                "is_error": is_error,
                            }
                        )
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            if response.stop_reason == "end_turn":
                final_text = "".join(
                    block.text
                    for block in response.content
                    if hasattr(block, "text")
                )
                return self._parse_response(final_text)

        raise AgentError(
            f"Agent exceeded maximum iterations ({self._max_iterations})"
        )

    # -- private ------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        template = (_PROMPTS_DIR / "system.txt").read_text(encoding="utf-8")
        now = datetime.now(ZoneInfo(TIMEZONE_NAME))
        current_datetime = now.strftime("%A, %B %d, %Y, %I:%M %p %Z")
        response_schema = json.dumps(
            RESPONSE_ADAPTER.json_schema(), indent=2
        )
        return template.format(
            current_datetime=current_datetime,
            response_schema=response_schema,
        )

    def _build_user_message(self, text: str, params: QueryParams) -> str:
        params_dict = {
            k: v
            for k, v in dataclasses.asdict(params).items()
            if v is not None
        }
        if params_dict:
            params_str = json.dumps(params_dict, indent=2)
            return f"{text}\n\nUser-provided filters:\n{params_str}"
        return text

    def _execute_tool(
        self, name: str, tool_input: dict[str, Any]
    ) -> tuple[str, bool]:
        """Returns (result_content, is_error)."""
        if name != "query_events":
            return (f"Unknown tool: {name}", True)
        try:
            params = QueryParams(
                days=tool_input.get("days"),
                from_date=tool_input.get("from_date"),
                to_date=tool_input.get("to_date"),
                min_guest=tool_input.get("min_guest", 50),
                max_guest=tool_input.get("max_guest"),
                min_time=tool_input.get("min_time"),
                max_time=tool_input.get("max_time"),
                day=tool_input.get("day"),
                exclude=tool_input.get("exclude"),
                search=tool_input.get("search"),
                regex=tool_input.get("regex"),
                glob=tool_input.get("glob"),
                sort=tool_input.get("sort", "date"),
                show_all=False,
            )
            result = self._store.query(params)
            return (json.dumps(result.events), False)
        except (QueryValidationError, CacheError) as exc:
            return (f"Tool error: {exc}", True)

    def _parse_response(self, text: str) -> AgentResult:
        cleaned = text.strip()
        fence_match = _MARKDOWN_FENCE_RE.match(cleaned)
        if fence_match:
            cleaned = fence_match.group(1)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise AgentError(
                f"Agent returned invalid JSON: {exc}\nResponse: {text[:500]}"
            ) from exc

        try:
            parsed = RESPONSE_ADAPTER.validate_python(data)
        except ValidationError as exc:
            raise AgentError(
                f"Agent response does not match schema: {exc}"
            ) from exc

        if isinstance(parsed, TextResponse):
            return TextResult(text=parsed.text)
        return EventListResult(events=parsed.events)
