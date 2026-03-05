"""Semantic filtering capability: agent must call list_events and apply semantic reasoning to produce an EventListResult."""

from __future__ import annotations

from pydantic_evals import Case, Dataset

from luma.agent import EventListResult
from luma.event_store import QueryParams

from evals.evaluators import Efficiency, EventIDsMatch, NDCGAtK
from evals.models import QueryInput
from evals.usecase.query_command._fixtures import FIXTURE_EVENTS

dataset = Dataset(
    name="query_command/semantic",
    cases=[
        Case(
            name="filter_ai_skip_wellness",
            inputs=QueryInput(
                prompt="Find me AI-related events, skip wellness stuff",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            # ev-ai-1: AI & ML Summit, ev-datascience-8: Data Science Happy Hour
            expected_output=EventListResult(ids=["ev-ai-1", "ev-datascience-8"]),
            metadata={"smoke": True},
        ),
        Case(
            name="tech_not_crypto",
            inputs=QueryInput(
                prompt="Show me tech events but not crypto",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            # ev-ai-1: [ai, tech], ev-datascience-8: [ai, data], ev-online-9: [tech, education]
            expected_output=EventListResult(ids=["ev-ai-1", "ev-datascience-8", "ev-online-9"]),
        ),
    ],
    evaluators=[
        EventIDsMatch(),
        NDCGAtK(),
        Efficiency(),
    ],
)
