"""LLM-powered agent with tool-calling loop."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol, Union

import anthropic
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from zoneinfo import ZoneInfo

from luma.config import (
    AGENT_LLM_TIMEOUT_SECONDS,
    AGENT_MAX_PARALLEL_TOOLS,
    AGENT_MAX_TOKENS,
    AGENT_TOOL_TIMEOUT_SECONDS,
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
    intro: str | None = None


@dataclass
class TextResult:
    text: str


AgentResult = EventListResult | TextResult


# ---------------------------------------------------------------------------
# Output types for query_iter (extensible)
# ---------------------------------------------------------------------------

@dataclass
class TextOutput:
    """Text block from the LLM (printed to stderr)."""

    text: str


@dataclass
class ToolFetchOutput:
    """Feedback when events were fetched (printed to stderr)."""

    count: int


@dataclass
class FinalResult:
    """Final agent result (printed to stdout)."""

    result: AgentResult


AgentOutput = TextOutput | ToolFetchOutput | FinalResult


class _Loader(Protocol):
    def start(self, label: str) -> None: ...
    def stop(self) -> None: ...


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
            "Returns matching events sorted by the specified criteria. "
            "When you need multiple independent queries (e.g. compare different date ranges), include all tool calls in one response."
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
        debug: bool = False,
    ) -> None:
        self._store = store
        self._model = model
        self._max_iterations = max_iterations
        self._debug = debug

    def run(self, messages: list[dict[str, str]]) -> Iterator[str]:
        _ = messages
        for token in self.RESPONSE.split():
            yield token

    def query_iter(
        self,
        text: str,
        params: QueryParams,
        *,
        loader: _Loader | None = None,
    ) -> Iterator[AgentOutput]:
        """Yields TextOutput, ToolFetchOutput at any point, FinalResult at the end."""
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
            if loader:
                loader.start("Thinking")
            try:
                t0 = time.perf_counter()
                response = self._create_llm_response(
                    client=client,
                    system_prompt=system_prompt,
                    messages=messages,
                )
                if self._debug:
                    elapsed = time.perf_counter() - t0
                    print(
                        f"[debug] LLM call: {elapsed:.2f}s, {len(messages)} messages",
                        file=sys.stderr,
                    )
            except anthropic.APIError as exc:
                if loader:
                    loader.stop()
                raise AgentError(f"Anthropic API error: {exc}") from exc
            finally:
                if loader:
                    loader.stop()

            # Yield text blocks from this turn (before tool_use)
            turn_text = "".join(
                block.text
                for block in response.content
                if hasattr(block, "text")
            )
            # Skip TextOutput for final turn; caller prints intro + formatted events
            if turn_text.strip() and response.stop_reason != "end_turn":
                yield TextOutput(text=turn_text.strip())

            if response.stop_reason == "tool_use":
                tool_use_blocks = [
                    block for block in response.content
                    if block.type == "tool_use"
                ]
                yield TextOutput(text="Searching events...")
                tool_results = []
                for batch_start in range(
                    0, len(tool_use_blocks), AGENT_MAX_PARALLEL_TOOLS
                ):
                    batch = tool_use_blocks[
                        batch_start : batch_start + AGENT_MAX_PARALLEL_TOOLS
                    ]
                    with ThreadPoolExecutor(
                        max_workers=AGENT_MAX_PARALLEL_TOOLS
                    ) as ex:
                        futures = [
                            ex.submit(self._execute_tool, block.name, block.input)
                            for block in batch
                        ]
                        for block, future in zip(batch, futures, strict=True):
                            try:
                                result_content, is_error = future.result(
                                    timeout=AGENT_TOOL_TIMEOUT_SECONDS
                                )
                            except FuturesTimeoutError:
                                result_content = (
                                    f"Tool {block.name} timed out after "
                                    f"{AGENT_TOOL_TIMEOUT_SECONDS}s"
                                )
                                is_error = True
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": result_content,
                                    "is_error": is_error,
                                }
                            )
                query_events_count = sum(
                    1 for b in tool_use_blocks if b.name == "query_events"
                )
                if query_events_count > 0:
                    yield ToolFetchOutput(count=query_events_count)

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            if response.stop_reason == "end_turn":
                result = self._parse_response(turn_text)
                yield FinalResult(result=result)
                return

        raise AgentError(
            f"Agent exceeded maximum iterations ({self._max_iterations})"
        )

    def query(self, text: str, params: QueryParams) -> AgentResult:
        """Backward-compatible: consume query_iter and return the last FinalResult."""
        last_result: AgentResult | None = None
        for item in self.query_iter(text, params):
            if isinstance(item, FinalResult):
                last_result = item.result
        if last_result is None:
            raise AgentError("Agent produced no result")
        return last_result

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

    def _create_llm_response(
        self,
        *,
        client: anthropic.Anthropic,
        system_prompt: str,
        messages: list[dict[str, Any]],
    ) -> Any:
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                client.messages.create,
                model=self._model,
                max_tokens=AGENT_MAX_TOKENS,
                system=system_prompt,
                messages=messages,
                tools=[QUERY_EVENTS_TOOL],
            )
            try:
                return future.result(timeout=AGENT_LLM_TIMEOUT_SECONDS)
            except FuturesTimeoutError as exc:
                raise AgentError(
                    "LLM response timed out after "
                    f"{AGENT_LLM_TIMEOUT_SECONDS}s"
                ) from exc

    def _execute_tool(
        self, name: str, tool_input: dict[str, Any]
    ) -> tuple[str, bool]:
        """Returns (result_content, is_error)."""
        if name != "query_events":
            return (f"Unknown tool: {name}", True)
        try:
            params = QueryParams.model_validate(tool_input)
            if self._debug:
                print(f"[debug] query_events params: {params.model_dump(exclude_none=True)}", file=sys.stderr)
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

        # Extract intro text before JSON for EventListResult
        intro: str | None = None
        if obj_match and obj_match.start() > 0:
            intro_text = cleaned[: obj_match.start()].strip()
            if intro_text:
                intro = intro_text

        return EventListResult(events=parsed.events, intro=intro)
