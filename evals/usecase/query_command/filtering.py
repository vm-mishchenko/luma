"""Filtering capability: agent must translate constraints (guest count, time, sort) into QueryParams."""

from __future__ import annotations

from pydantic_evals import Case, Dataset

from luma.agent import QueryParamsResult
from luma.event_store import QueryParams

from evals.evaluators import Efficiency, NoUnnecessaryToolUse, ParamsMatch, ResultTypeMatch
from evals.models import QueryInput
from evals.usecase.query_command._fixtures import FIXTURE_EVENTS

dataset = Dataset(
    name="query_command/filtering",
    cases=[
        Case(
            name="guest_filter",
            inputs=QueryInput(
                prompt="Popular events with over 100 guests",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(min_guest=100)),
            metadata={"smoke": True},
        ),
        Case(
            name="max_guest",
            inputs=QueryInput(
                prompt="Small intimate events under 50 guests",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(max_guest=50)),
        ),
        Case(
            name="evening_events",
            inputs=QueryInput(
                prompt="Evening events tomorrow",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(
                params=QueryParams(range="tomorrow", min_time=17)
            ),
        ),
        Case(
            name="sort_by_popularity",
            inputs=QueryInput(
                prompt="Most popular events this week",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(sort="guest")),
        ),
        Case(
            name="combined_filters",
            inputs=QueryInput(
                prompt="Big events this weekend in SF",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(
                params=QueryParams(range="weekend", min_guest=100, city="San Francisco")
            ),
        ),
    ],
    evaluators=[
        ResultTypeMatch(),
        ParamsMatch(),
        NoUnnecessaryToolUse(),
        Efficiency(),
    ],
)
