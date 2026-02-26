"""Domain-specific input model for eval cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from query import QueryParams


@dataclass
class QueryInput:
    prompt: str
    params: QueryParams
    events: list[dict[str, Any]]
