"""LLM-powered agent with tool-calling loop."""

from __future__ import annotations

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
# Tool schema (generated from QueryParams)
# ---------------------------------------------------------------------------

def _build_tool_schema() -> dict[str, Any]:
    schema = QueryParams.model_json_schema()
    schema.pop("title", None)
    for key in ("show_all",):
        schema.get("properties", {}).pop(key, None)
    return {
        "name": "query_events",
        "description": (
            "Search and filter events from the database. "
            "Returns matching events sorted by the specified criteria."
        ),
        "input_schema": schema,
    }


QUERY_EVENTS_TOOL: dict[str, Any] = _build_tool_schema()

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_MARKDOWN_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


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
        template = (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
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
        params_dict = params.model_dump(exclude_none=True)
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
            params = QueryParams.model_validate(tool_input)
            result = self._store.query(params)
            return (json.dumps(result.events), False)
        except (ValidationError, QueryValidationError, CacheError) as exc:
            return (f"Tool error: {exc}", True)

    def _parse_response(self, text: str) -> AgentResult:
        cleaned = text.strip()

        # Try full text first, then fenced block, then first JSON object.
        candidates = [cleaned]
        fence_match = _MARKDOWN_FENCE_RE.search(cleaned)
        if fence_match:
            candidates.append(fence_match.group(1))
        obj_match = _JSON_OBJECT_RE.search(cleaned)
        if obj_match:
            candidates.append(obj_match.group(0))

        data = None
        last_exc: json.JSONDecodeError | None = None
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError as exc:
                last_exc = exc

        if data is None:
            raise AgentError(
                f"Agent returned invalid JSON: {last_exc}\nResponse: {text[:500]}"
            ) from last_exc

        try:
            parsed = RESPONSE_ADAPTER.validate_python(data)
        except ValidationError as exc:
            raise AgentError(
                f"Agent response does not match schema: {exc}"
            ) from exc

        if isinstance(parsed, TextResponse):
            return TextResult(text=parsed.text)
        return EventListResult(events=parsed.events)
