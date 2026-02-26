"""Stateless chat agent interface."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from luma.event_store import EventStore, QueryParams


@dataclass
class EventListResult:
    events: list[dict[str, Any]]


@dataclass
class TextResult:
    text: str


AgentResult = EventListResult | TextResult


class Agent:
    """Fake agent used to validate streaming contract."""

    RESPONSE = "I'm Luma assistant. I can help you find events."

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def run(self, messages: list[dict[str, str]]) -> Iterator[str]:
        _ = messages
        for token in self.RESPONSE.split():
            yield token

    def query(self, text: str, params: QueryParams) -> AgentResult:
        _ = text, params
        return TextResult(text=self.RESPONSE)
