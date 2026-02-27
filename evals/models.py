"""Domain-specific input model for eval cases."""

from __future__ import annotations

from dataclasses import dataclass

from luma.event_store import QueryParams
from luma.models import Event


@dataclass
class QueryInput:
    prompt: str
    params: QueryParams
    events: list[Event]
