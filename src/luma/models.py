"""Shared domain models for luma."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def generate_event_id(url: str) -> str:
    """Stable 6-char hex hash derived from event URL."""
    return hashlib.md5(url.encode()).hexdigest()[:6]


@dataclass
class Event:
    id: str
    title: str
    url: str
    start_at: str
    guest_count: int
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "start_at": self.start_at,
            "guest_count": self.guest_count,
            "sources": self.sources,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Event:
        return Event(
            id=d.get("id") or generate_event_id(d["url"]),
            title=d["title"],
            url=d["url"],
            start_at=d["start_at"],
            guest_count=d["guest_count"],
            sources=d.get("sources", []),
        )
