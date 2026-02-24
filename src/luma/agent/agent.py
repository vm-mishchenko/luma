"""Stateless chat agent interface."""

from __future__ import annotations

from collections.abc import Iterator


class Agent:
    """Fake agent used to validate streaming contract."""

    RESPONSE = "I'm Luma assistant. I can help you find events."

    def run(self, messages: list[dict[str, str]]) -> Iterator[str]:
        _ = messages
        for token in self.RESPONSE.split():
            yield token
