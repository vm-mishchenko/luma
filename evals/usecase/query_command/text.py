"""Text response capability: agent must answer in natural language when no structured query is appropriate."""

from __future__ import annotations

from pydantic_evals import Case, Dataset

from luma.agent import TextResult
from luma.event_store import QueryParams

from evals.evaluators import Efficiency, TextNotEmpty
from evals.models import QueryInput
from evals.usecase.query_command._fixtures import FIXTURE_EVENTS

dataset = Dataset(
    name="query_command/text",
    cases=[
        Case(
            name="count_question",
            inputs=QueryInput(
                prompt="How many events are there this week?",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=TextResult(text=""),
            metadata={"smoke": True},
        ),
        Case(
            name="comparison_query",
            inputs=QueryInput(
                prompt="Are there more events on Saturday or Sunday?",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=TextResult(text=""),
        ),
        Case(
            name="irrelevant_query",
            inputs=QueryInput(
                prompt="What's the weather like?",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=TextResult(text=""),
        ),
        Case(
            name="vague_query",
            inputs=QueryInput(
                prompt="Show me something fun",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=TextResult(text=""),
        ),
        Case(
            name="empty_fixture",
            inputs=QueryInput(
                prompt="What events are available this week?",
                params=QueryParams(),
                events=[],
            ),
            expected_output=TextResult(text=""),
        ),
    ],
    evaluators=[
        TextNotEmpty(),
        Efficiency(),
    ],
)
