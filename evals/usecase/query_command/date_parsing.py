"""Date parsing capability: agent must translate natural-language time expressions into QueryParams."""

from __future__ import annotations

from pydantic_evals import Case, Dataset

from luma.agent import QueryParamsResult
from luma.event_store import QueryParams

from evals.evaluators import Efficiency, NoUnnecessaryToolUse, ParamsMatch, ResultTypeMatch
from evals.models import QueryInput
from evals.usecase.query_command._fixtures import FIXTURE_EVENTS

dataset = Dataset(
    name="query_command/date_parsing",
    cases=[
        Case(
            name="range_tomorrow",
            inputs=QueryInput(
                prompt="What's happening tomorrow?",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(range="tomorrow")),
            metadata={"smoke": True},
        ),
        Case(
            name="range_weekend",
            inputs=QueryInput(
                prompt="Events this weekend",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(range="weekend")),
        ),
        Case(
            name="range_next_week",
            inputs=QueryInput(
                prompt="What's happening next week?",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(range="week+1")),
        ),
        Case(
            name="range_weekday",
            inputs=QueryInput(
                prompt="Weekday events this week",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(range="weekday")),
        ),
        Case(
            name="specific_date",
            inputs=QueryInput(
                prompt="Events on March 15",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(
                params=QueryParams(from_date="20260315", to_date="20260315")
            ),
        ),
        Case(
            name="range_today",
            inputs=QueryInput(
                prompt="What's happening today?",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(range="today")),
        ),
    ],
    evaluators=[
        ResultTypeMatch(),
        ParamsMatch(),
        NoUnnecessaryToolUse(),
        Efficiency(),
    ],
)
