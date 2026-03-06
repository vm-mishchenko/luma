"""Event detail capability: agent must fetch event details when asked about a specific event."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic_evals import Case, Dataset

from luma.agent import TextResult
from luma.event_store import QueryParams
from luma.models import Event

from evals.evaluators import Efficiency, ToolUsed, TextNotEmpty
from evals.models import QueryInput

_now = datetime.now(timezone.utc)

_DETAIL_EVENTS = [
    Event(
        id="evt-6SJJumZ2T5iWsrH",
        title='"Timing Meets Conviction" - An Invitation by Beillion & 922 Capital',
        url="https://luma.com/k7ekw70a",
        start_at=(_now + timedelta(days=1)).isoformat(),
        guest_count=34,
        city="San Francisco",
        location_type="offline",
        sources=["category:ai"],
    ),
    Event(
        id="evt-mDKhAgwpEqoIQBC",
        title="East Bay AI Club - AI Product Launch and Demo",
        url="https://luma.com/east-bay-ai-club",
        start_at=(_now + timedelta(days=2)).isoformat(),
        guest_count=70,
        city="San Francisco",
        location_type="offline",
        sources=["category:ai", "category:tech"],
    ),
]

dataset = Dataset(
    name="query_command/event_detail",
    cases=[
        Case(
            name="what_is_event_about",
            inputs=QueryInput(
                prompt="What is the Timing Meets Conviction event about?",
                params=QueryParams(),
                events=_DETAIL_EVENTS,
            ),
            expected_output=TextResult(text=""),
            metadata={"smoke": True},
        ),
        Case(
            name="tell_me_more",
            inputs=QueryInput(
                prompt="Tell me more about the East Bay AI Club event",
                params=QueryParams(),
                events=_DETAIL_EVENTS,
            ),
            expected_output=TextResult(text=""),
            metadata={},
        ),
    ],
    evaluators=[
        TextNotEmpty(),
        ToolUsed(tool_name="get_event_detail"),
        Efficiency(token_budget=20000, time_budget=30.0),
    ],
)
