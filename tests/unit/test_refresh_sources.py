"""Tests for [refresh] config: validate_config and get_refresh_sources."""

from __future__ import annotations

import pytest

from luma.user_config import (
    DEFAULT_REFRESH_CALENDARS,
    DEFAULT_REFRESH_CATEGORIES,
    get_refresh_sources,
    validate_config,
)


def test_no_refresh_section_empty() -> None:
    validate_config({})
    cats, cals = get_refresh_sources({})
    assert cats == []
    assert cals == []


def test_explicit_refresh_same_as_shipped_template_defaults() -> None:
    cfg = {
        "refresh": {
            "categories": list(DEFAULT_REFRESH_CATEGORIES),
            "calendars": [
                {"url": c["url"], "calendar_api_id": c["calendar_api_id"]}
                for c in DEFAULT_REFRESH_CALENDARS
            ],
        }
    }
    validate_config(cfg)
    cats, cals = get_refresh_sources(cfg)
    assert cats == list(DEFAULT_REFRESH_CATEGORIES)
    assert cals == [
        {"url": c["url"], "calendar_api_id": c["calendar_api_id"]}
        for c in DEFAULT_REFRESH_CALENDARS
    ]


def test_refresh_only_categories_calendars_empty() -> None:
    cfg = {
        "refresh": {
            "categories": ["https://luma.com/ai"],
        }
    }
    validate_config(cfg)
    cats, cals = get_refresh_sources(cfg)
    assert cats == ["https://luma.com/ai"]
    assert cals == []


def test_refresh_only_calendars_categories_empty() -> None:
    cfg = {
        "refresh": {
            "calendars": [
                {"url": "https://luma.com/x", "calendar_api_id": "cal-1"},
            ],
        }
    }
    validate_config(cfg)
    cats, cals = get_refresh_sources(cfg)
    assert cats == []
    assert cals == [{"url": "https://luma.com/x", "calendar_api_id": "cal-1"}]


def test_empty_categories_keeps_calendars() -> None:
    cfg = {
        "refresh": {
            "categories": [],
            "calendars": [{"url": "https://luma.com/z", "calendar_api_id": "cal-z"}],
        }
    }
    validate_config(cfg)
    cats, cals = get_refresh_sources(cfg)
    assert cats == []
    assert len(cals) == 1


def test_dedupe_categories_and_calendars() -> None:
    cfg = {
        "refresh": {
            "categories": [
                "https://luma.com/ai",
                "https://luma.com/ai",
                "https://luma.com/tech",
            ],
            "calendars": [
                {"url": "https://luma.com/a", "calendar_api_id": "cal-a"},
                {"url": "https://luma.com/a", "calendar_api_id": "cal-b"},
            ],
        }
    }
    validate_config(cfg)
    cats, cals = get_refresh_sources(cfg)
    assert cats == ["https://luma.com/ai", "https://luma.com/tech"]
    assert cals == [{"url": "https://luma.com/a", "calendar_api_id": "cal-a"}]


def test_calendar_row_extra_keys_ignored() -> None:
    cfg = {
        "refresh": {
            "calendars": [
                {
                    "url": "https://luma.com/c",
                    "calendar_api_id": "cal-c",
                    "note": "ignored",
                },
            ],
        }
    }
    validate_config(cfg)
    _, cals = get_refresh_sources(cfg)
    assert cals == [{"url": "https://luma.com/c", "calendar_api_id": "cal-c"}]


def test_validate_categories_not_list_exits() -> None:
    with pytest.raises(SystemExit) as exc:
        validate_config({"refresh": {"categories": "bad"}})
    assert exc.value.code == 2


def test_validate_calendar_row_missing_url_exits() -> None:
    with pytest.raises(SystemExit) as exc:
        validate_config({"refresh": {"calendars": [{"calendar_api_id": "cal-x"}]}})
    assert exc.value.code == 2


def test_validate_calendar_api_id_wrong_type_exits() -> None:
    with pytest.raises(SystemExit) as exc:
        validate_config(
            {"refresh": {"calendars": [{"url": "https://luma.com/u", "calendar_api_id": 1}]}}
        )
    assert exc.value.code == 2
