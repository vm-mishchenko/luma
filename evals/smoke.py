"""Smoke eval set: basic sanity checks for Agent.query."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic_evals import Case, Dataset

from luma.event_store import QueryParams
from luma.models import Event

from .evaluators import NotEmpty
from .models import QueryInput

FIXTURE_EVENTS = [
    Event(
        id="evt-eval1",
        title="AI Meetup",
        url="https://lu.ma/ai-meetup",
        start_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        guest_count=120,
        sources=["category:ai"],
    ),
    Event(
        id="evt-eval2",
        title="Yoga in the Park",
        url="https://lu.ma/yoga",
        start_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        guest_count=30,
        sources=["category:wellness"],
    ),
]

dataset = Dataset(
    name="smoke",
    cases=[
        Case(
            name="text_response_basic",
            inputs=QueryInput(
                prompt="What events are happening?",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
        ),
        Case(
            name="filter_large_events",
            inputs=QueryInput(
                prompt="Show me popular events with over 100 guests",
                params=QueryParams(min_guest=100),
                events=FIXTURE_EVENTS,
            ),
        ),
    ],
    evaluators=[NotEmpty()],
)
