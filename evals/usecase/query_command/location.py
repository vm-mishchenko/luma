"""Location capability: agent must resolve city names, coordinates, and location type into QueryParams."""

from __future__ import annotations

from pydantic_evals import Case, Dataset

from luma.agent import QueryParamsResult
from luma.event_store import QueryParams

from evals.evaluators import (
    CoordinatesSet,
    Efficiency,
    NoUnnecessaryToolUse,
    ParamsMatch,
    ResultTypeMatch,
)
from evals.models import QueryInput
from evals.usecase.query_command._fixtures import FIXTURE_EVENTS

dataset = Dataset(
    name="query_command/location",
    cases=[
        Case(
            name="city_sf",
            inputs=QueryInput(
                prompt="Events in SF",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(city="San Francisco")),
            metadata={"smoke": True},
        ),
        Case(
            name="city_sf_offline",
            inputs=QueryInput(
                prompt="In-person events in San Francisco",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(
                params=QueryParams(location_type="offline", city="San Francisco")
            ),
        ),
        Case(
            name="online_events",
            inputs=QueryInput(
                prompt="Online events this week",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            expected_output=QueryParamsResult(params=QueryParams(location_type="online")),
        ),
        Case(
            name="coordinates_stanford",
            inputs=QueryInput(
                prompt="Events near Stanford",
                params=QueryParams(),
                events=FIXTURE_EVENTS,
            ),
            # No expected params — CoordinatesSet checks that the agent set lat/lon.
            expected_output=QueryParamsResult(params=QueryParams()),
        ),
    ],
    evaluators=[
        ResultTypeMatch(),
        ParamsMatch(),
        CoordinatesSet(),
        NoUnnecessaryToolUse(),
        Efficiency(),
    ],
)
