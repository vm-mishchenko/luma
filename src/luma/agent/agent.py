"""LLM-powered agent with tool-calling loop."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, Protocol, Union

import anthropic
import logfire
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from zoneinfo import ZoneInfo

logfire.configure(send_to_logfire=False)

from luma.agent.tool import Tool, ToolResult
from luma.config import (
    AGENT_LLM_TIMEOUT_SECONDS,
    AGENT_MAX_PARALLEL_TOOLS,
    AGENT_MAX_TOKENS,
    AGENT_TOOL_TIMEOUT_SECONDS,
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_AGENT_MAX_ITERATIONS,
    DEFAULT_AGENT_MODEL,
    DEFAULT_SORT,
    TIMEZONE_NAME,
)
from luma.event_store import QueryParams


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
    ids: list[str]


@dataclass
class TextResult:
    text: str


@dataclass
class QueryParamsResult:
    params: QueryParams


AgentResult = EventListResult | TextResult | QueryParamsResult


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
    ids: list[str]


class AgentQueryParams(BaseModel):
    """QueryParams subset exposed to the LLM (tool input and query response)."""
    days: int | None = Field(None, description="Window size in days starting from today. days=1 means today only, days=2 means today and tomorrow. For a specific date use from_date/to_date.")
    from_date: str | None = Field(None, description="Start date YYYYMMDD (inclusive). Mutually exclusive with days.")
    to_date: str | None = Field(None, description="End date YYYYMMDD (inclusive). Mutually exclusive with days.")
    min_guest: int | None = Field(None, description="Minimum guest count.")
    max_guest: int | None = Field(None, description="Maximum guest count.")
    min_time: int | None = Field(None, description="Minimum start hour in LA time (0-23).")
    max_time: int | None = Field(None, description="Maximum start hour in LA time (0-23).")
    day: str | None = Field(None, description="Comma-separated weekday filter, e.g. 'Sat,Sun'.")
    sort: Literal["date", "guest"] | None = Field(None, description="Sort by 'date' (default) or 'guest'.")
    range: str | None = Field(None, description="Predefined date range. Values: today, tomorrow, week[+N], weekday[+N], weekend[+N]. Week is Mon-Sun. Mutually exclusive with days, from_date, to_date.")
    city: str | None = Field(None, description="Filter by city name (case-insensitive exact match). Mutually exclusive with search_lat/search_lon.")
    region: str | None = Field(None, description="Filter by region/state.")
    country: str | None = Field(None, description="Filter by country.")
    location_type: str | None = Field(None, description="Filter by location type: 'offline', 'online'.")
    search_lat: float | None = Field(None, description="Latitude of search center. Provide approximate coordinates for location-based queries. Requires search_lon. Mutually exclusive with city.")
    search_lon: float | None = Field(None, description="Longitude of search center. Requires search_lat. Mutually exclusive with city.")
    search_radius_miles: float | None = Field(None, description="Search radius in miles (default: 5). Requires search_lat and search_lon.")


def _to_query_params(agent_params: AgentQueryParams) -> QueryParams:
    return QueryParams(
        days=agent_params.days,
        from_date=agent_params.from_date,
        to_date=agent_params.to_date,
        min_guest=agent_params.min_guest,
        max_guest=agent_params.max_guest,
        min_time=agent_params.min_time,
        max_time=agent_params.max_time,
        day=agent_params.day,
        sort=agent_params.sort or DEFAULT_SORT,
        range=agent_params.range,
        city=agent_params.city,
        region=agent_params.region,
        country=agent_params.country,
        location_type=agent_params.location_type,
        search_lat=agent_params.search_lat,
        search_lon=agent_params.search_lon,
        search_radius_miles=agent_params.search_radius_miles,
    )


class QueryResponse(BaseModel):
    type: Literal["query"]
    params: AgentQueryParams


RESPONSE_ADAPTER: TypeAdapter[TextResponse | EventsResponse | QueryResponse] = TypeAdapter(
    Annotated[
        Union[TextResponse, EventsResponse, QueryResponse],
        Field(discriminator="type"),
    ]
)


_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_MARKDOWN_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


# ---------------------------------------------------------------------------
# Prompt & response helpers (used by callers to configure the Agent)
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Build the default system prompt for the query/chat agent."""
    template = (_PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
    now = datetime.now(ZoneInfo(TIMEZONE_NAME))
    current_datetime = now.strftime("%A, %B %d, %Y, %I:%M %p %Z")
    current_date = now.strftime("%Y%m%d")
    response_schema = json.dumps(RESPONSE_ADAPTER.json_schema(), indent=2)

    tomorrow = now + timedelta(days=1)
    days_until_saturday = (5 - now.weekday()) % 7 or 7
    saturday = now + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)
    next_monday = saturday + timedelta(days=2)
    next_sunday = next_monday + timedelta(days=6)
    fmt = "%Y%m%d"

    return template.format(
        current_datetime=current_datetime,
        current_date=current_date,
        response_schema=response_schema,
        tomorrow=tomorrow.strftime(fmt),
        saturday=saturday.strftime(fmt),
        sunday=sunday.strftime(fmt),
        next_monday=next_monday.strftime(fmt),
        next_sunday=next_sunday.strftime(fmt),
    )


def build_user_message(text: str, params: QueryParams) -> str:
    """Build the user message, appending query params when present."""
    params_dict = params.model_dump(exclude_none=True)
    if params_dict:
        params_str = json.dumps(params_dict, indent=2)
        return f"{text}\n\nUser-provided filters:\n{params_str}"
    return text


def parse_agent_response(data: Any) -> AgentResult:
    """Validate and map LLM JSON to AgentResult using the default response schema."""
    try:
        parsed = RESPONSE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise AgentError(
            f"Agent response does not match schema: {exc}"
        ) from exc

    if isinstance(parsed, TextResponse):
        return TextResult(text=parsed.text)
    if isinstance(parsed, QueryResponse):
        return QueryParamsResult(params=_to_query_params(parsed.params))
    return EventListResult(ids=parsed.ids)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    """Generic LLM executor with optional tool-calling loop."""

    RESPONSE = "I'm Luma assistant. I can help you find events."

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: list[Tool],
        expected_output: Callable[[Any], AgentResult],
        model: str = DEFAULT_AGENT_MODEL,
        max_iterations: int = DEFAULT_AGENT_MAX_ITERATIONS,
        debug: bool = False,
    ) -> None:
        self._system_prompt = system_prompt
        self._tools_by_name: dict[str, Tool] = {t.name: t for t in tools}
        self._tools_schema: list[dict[str, Any]] | None = (
            [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]
            if tools
            else None
        )
        self._expected_output = expected_output
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
        *,
        loader: _Loader | None = None,
    ) -> Iterator[AgentOutput]:
        """Yields TextOutput, ToolFetchOutput at any point, FinalResult at the end."""
        with logfire.span("agent.run"):
            api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
            if not api_key:
                raise AgentError(
                    f"Environment variable {ANTHROPIC_API_KEY_ENV} is not set. "
                    "Set it to your Anthropic API key to use the agent."
                )

            client = anthropic.Anthropic(api_key=api_key)
            messages: list[dict[str, Any]] = [{"role": "user", "content": text}]

            for _ in range(self._max_iterations):
                if loader and not self._debug:
                    loader.start("Thinking")
                try:
                    t0 = time.perf_counter()
                    if self._debug:
                        msg_chars = sum(
                            len(str(m.get("content", ""))) for m in messages
                        )
                        print(
                            f"[debug] Start LLM call: {len(messages)} messages"
                            f"(system={len(self._system_prompt)}, messages={msg_chars})",
                            file=sys.stderr,
                        )
                    with logfire.span("agent.llm_call") as llm_span:
                        response = self._create_llm_response(
                            client=client,
                            messages=messages,
                        )
                        llm_span.set_attribute("input_tokens", response.usage.input_tokens)
                        llm_span.set_attribute("output_tokens", response.usage.output_tokens)
                        llm_span.set_attribute("model", self._model)
                        llm_span.set_attribute("stop_reason", response.stop_reason)
                    if self._debug:
                        elapsed = time.perf_counter() - t0
                        print(
                            f"[debug] End LLM call: {elapsed:.2f}s, {len(messages)} messages",
                            file=sys.stderr,
                        )
                except anthropic.APIError as exc:
                    if loader:
                        loader.stop()
                    raise AgentError(f"Anthropic API error: {exc}") from exc
                finally:
                    if loader:
                        loader.stop()

                turn_text = "".join(
                    block.text
                    for block in response.content
                    if hasattr(block, "text")
                )

                if response.stop_reason == "end_turn":
                    result = self._parse_response(turn_text)
                    if self._debug:
                        label = type(result).__name__
                        if isinstance(result, QueryParamsResult):
                            label += f" {result.params.model_dump(exclude_none=True)}"
                        print(f"[debug] response type: {label}", file=sys.stderr)
                    yield FinalResult(result=result)
                    return

                if response.stop_reason == "tool_use":
                    if not self._tools_by_name:
                        raise AgentError(
                            "LLM requested tool use but no tools are configured"
                        )

                    tool_use_blocks = [
                        block for block in response.content
                        if block.type == "tool_use"
                    ]
                    if self._debug:
                        for b in tool_use_blocks:
                            print(f"[debug] tool call: {b.name} {b.input}", file=sys.stderr)

                    if turn_text.strip():
                        yield TextOutput(text=turn_text.strip())

                    counts: dict[str, int] = {}
                    for b in tool_use_blocks:
                        counts[b.name] = counts.get(b.name, 0) + 1
                    parts: list[str] = []
                    for tool_name, count in counts.items():
                        tool = self._tools_by_name.get(tool_name)
                        if tool:
                            msg = tool.loading_message
                            if count > 1:
                                msg = f"{msg} ({count} queries)"
                            parts.append(msg)
                    if parts:
                        yield TextOutput(text=f"{', '.join(parts)}...")

                    tool_results = []
                    executed_results: list[ToolResult] = []
                    for batch_start in range(
                        0, len(tool_use_blocks), AGENT_MAX_PARALLEL_TOOLS
                    ):
                        batch = tool_use_blocks[
                            batch_start : batch_start + AGENT_MAX_PARALLEL_TOOLS
                        ]

                        def _execute_with_span(block: Any) -> ToolResult:
                            with logfire.span("agent.tool_call") as tool_span:
                                res = self._execute_tool(block.name, block.input)
                                tool_span.set_attribute("tool_name", block.name)
                                tool_span.set_attribute("is_error", res.is_error)
                            return res

                        with ThreadPoolExecutor(
                            max_workers=AGENT_MAX_PARALLEL_TOOLS
                        ) as ex:
                            futures = [
                                ex.submit(_execute_with_span, block)
                                for block in batch
                            ]
                            for block, future in zip(batch, futures, strict=True):
                                try:
                                    result = future.result(
                                        timeout=AGENT_TOOL_TIMEOUT_SECONDS
                                    )
                                except FuturesTimeoutError:
                                    result = ToolResult(
                                        content=(
                                            f"Tool {block.name} timed out after "
                                            f"{AGENT_TOOL_TIMEOUT_SECONDS}s"
                                        ),
                                        is_error=True,
                                    )
                                executed_results.append(result)
                                tool_results.append(
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": result.content,
                                        "is_error": result.is_error,
                                    }
                                )
                    fetch_count = sum(
                        1
                        for r in executed_results
                        if r.metadata and r.metadata.get("fetch")
                    )
                    if fetch_count > 0:
                        yield ToolFetchOutput(count=fetch_count)

                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                    continue

            raise AgentError(
                f"Agent exceeded maximum iterations ({self._max_iterations})"
            )

    def query(self, text: str) -> AgentResult:
        """Consume query_iter and return the final result."""
        last_result: AgentResult | None = None
        for item in self.query_iter(text):
            if isinstance(item, FinalResult):
                last_result = item.result
        if last_result is None:
            raise AgentError("Agent produced no result")
        return last_result

    # -- private ------------------------------------------------------------

    def _create_llm_response(
        self,
        *,
        client: anthropic.Anthropic,
        messages: list[dict[str, Any]],
    ) -> Any:
        kwargs: dict[str, Any] = dict(
            model=self._model,
            max_tokens=AGENT_MAX_TOKENS,
            system=self._system_prompt,
            messages=messages,
        )
        if self._tools_schema:
            kwargs["tools"] = self._tools_schema
        with ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(client.messages.create, **kwargs)
            try:
                return future.result(timeout=AGENT_LLM_TIMEOUT_SECONDS)
            except FuturesTimeoutError as exc:
                raise AgentError(
                    "LLM response timed out after "
                    f"{AGENT_LLM_TIMEOUT_SECONDS}s"
                ) from exc

    def _execute_tool(
        self, name: str, tool_input: dict[str, Any]
    ) -> ToolResult:
        tool = self._tools_by_name.get(name)
        if tool is None:
            return ToolResult(content=f"Unknown tool: {name}", is_error=True)
        return tool.execute(tool_input)

    def _parse_response(self, text: str) -> AgentResult:
        cleaned = text.strip()

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
            return self._expected_output(data)
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError(
                f"Agent response does not match schema: {exc}"
            ) from exc
