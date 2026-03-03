"""Tool Protocol and ToolResult for the agent tool-calling loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolResult:
    content: str
    is_error: bool
    metadata: dict[str, Any] | None = field(default=None)


@runtime_checkable
class Tool(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def input_schema(self) -> dict[str, Any]: ...

    @property
    def loading_message(self) -> str: ...

    def execute(self, tool_input: dict[str, Any]) -> ToolResult: ...
