"""E2E tests for the luma CLI.

Each test exercises the full CLI path: argv -> parse_args -> config.configure
-> command dispatch -> filesystem side-effects / stdout / stderr.

The *only* mock seam is ``download.download_events``; everything else
(config, refresh orchestration, query, cache I/O) runs for real.

Skipped white-box assertions (not externally observable via CLI):
- parse_args() internal namespace shape (``args.command is None``).
- find_latest_cache() return object (implementation detail).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import luma.cli as cli
import luma.config as config
from luma.models import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    Event(
        id="evt-test1",
        title="AI Meetup",
        url="https://luma.com/ai-meetup",
        start_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        guest_count=120,
        sources=["category:ai"],
        city="San Francisco",
        region="California",
        country="US",
        location_type="offline",
        latitude=37.78,
        longitude=-122.42,
    ),
    Event(
        id="evt-test2",
        title="Tech Talk",
        url="https://luma.com/tech-talk",
        start_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        guest_count=80,
        sources=["category:tech"],
    ),
    Event(
        id="evt-test3",
        title="Small Event",
        url="https://luma.com/small-event",
        start_at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        guest_count=10,
        sources=["category:tech"],
    ),
]


def _write_cache(tmp_path, events=None, fetched_at=None):
    """Write a minimal cache file directly into *tmp_path*."""
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)
    if events is None:
        events = SAMPLE_EVENTS
    stamp = fetched_at.strftime("%Y-%m-%d_%H-%M-%S")
    path = tmp_path / f"events-{stamp}.json"
    payload = {
        "fetched_at": fetched_at.isoformat(),
        "events": [e.to_dict() for e in events],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _run_cli(argv):
    """Run CLI with given argv list, return exit code."""
    with mock.patch.object(sys, "argv", ["luma"] + list(argv)):
        try:
            return cli.main()
        except SystemExit as exc:
            return exc.code


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_config():
    """Ensure config overrides do not leak between tests."""
    yield
    config._reset()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_query_no_cache_exits_1(tmp_path, capsys):
    rc = _run_cli(["--cache-dir", str(tmp_path)])
    assert rc == 1
    assert "No cached events" in capsys.readouterr().err


def test_refresh_success(tmp_path, capsys):
    with mock.patch("luma.refresh.download_events", return_value=SAMPLE_EVENTS):
        rc = _run_cli(["--cache-dir", str(tmp_path), "refresh"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Cached 3 events" in err

    cache_files = list(tmp_path.glob("events-*.json"))
    assert len(cache_files) == 1


def test_refresh_network_error(tmp_path, capsys):
    with mock.patch("luma.refresh.download_events", side_effect=OSError("net down")):
        rc = _run_cli(["--cache-dir", str(tmp_path), "refresh"])
    assert rc == 1
    assert "net down" in capsys.readouterr().err


def test_query_with_cache(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--min-guest", "0"])
    assert rc == 0
    assert "Top" in capsys.readouterr().out


def test_stale_cache_warns(tmp_path, capsys):
    stale_time = datetime.now(timezone.utc) - timedelta(days=3)
    _write_cache(tmp_path, fetched_at=stale_time)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--min-guest", "0"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Run 'luma refresh'" in err


def test_refresh_retries_forwarded(tmp_path):
    with mock.patch("luma.refresh.download_events", return_value=[]) as m:
        _run_cli(["--cache-dir", str(tmp_path), "refresh", "--retries", "3"])
    assert m.call_args.kwargs["retries"] == 3


def test_retries_on_main_parser_rejected():
    rc = _run_cli(["--retries", "3"])
    assert rc == 2


def test_days_on_refresh(tmp_path):
    with mock.patch("luma.refresh.download_events", return_value=[]) as m:
        _run_cli(["--cache-dir", str(tmp_path), "refresh", "--days", "7"])
    assert m.call_args.kwargs["end_utc"] - m.call_args.kwargs["start_utc"] == timedelta(days=7)


def test_discard_writes_seen(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--discard", "--min-guest", "0"])
    assert rc == 0
    seen_path = tmp_path / "seen.json"
    assert seen_path.is_file()
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
    assert len(seen) > 0


def test_show_all_includes_seen(tmp_path, capsys):
    _write_cache(tmp_path)

    seen_path = tmp_path / "seen.json"
    seen_path.write_text(
        json.dumps(["https://luma.com/ai-meetup"]), encoding="utf-8"
    )

    rc = _run_cli(["--cache-dir", str(tmp_path), "--all", "--min-guest", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out


# ---------------------------------------------------------------------------
# Location filter tests
# ---------------------------------------------------------------------------


def test_city_filter(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--city", "San Francisco"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out
    assert "Tech Talk" not in out
    assert "Small Event" not in out


def test_sf_shortcut(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--sf"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out
    assert "Tech Talk" not in out


def test_sf_overrides_city(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--sf", "--city", "Oakland"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out
    assert "Tech Talk" not in out


def test_location_type_filter(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--location-type", "offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out
    assert "Tech Talk" not in out


def test_city_case_insensitive(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--city", "san francisco"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out


def test_city_excludes_none(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--city", "San Francisco"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tech Talk" not in out
    assert "Small Event" not in out


# ---------------------------------------------------------------------------
# Free-text query (Agent) tests
# ---------------------------------------------------------------------------


def test_free_text_prints_agent_response(tmp_path, capsys):
    from luma.agent.agent import FinalResult, TextResult

    def _mock_query_iter(*args, **kwargs):
        yield FinalResult(result=TextResult(text="Here are your events."))

    with mock.patch("luma.agent.agent.Agent.query_iter", side_effect=_mock_query_iter):
        rc = _run_cli(["--cache-dir", str(tmp_path), "hello"])
    assert rc == 0
    assert "Here are your events." in capsys.readouterr().out


def test_free_text_with_flags(tmp_path, capsys):
    from luma.agent.agent import FinalResult, TextResult

    def _mock_query_iter(*args, **kwargs):
        yield FinalResult(result=TextResult(text="Here are your events."))

    with mock.patch("luma.agent.agent.Agent.query_iter", side_effect=_mock_query_iter):
        rc = _run_cli(["--cache-dir", str(tmp_path), "--days", "7", "hello"])
    assert rc == 0
    assert "Here are your events." in capsys.readouterr().out


def test_empty_free_text_falls_through(tmp_path, capsys):
    rc = _run_cli(["--cache-dir", str(tmp_path), ""])
    assert rc == 1
    assert "No cached events" in capsys.readouterr().err


def test_free_text_event_list_result(tmp_path, capsys):
    from luma.agent.agent import EventListResult, FinalResult

    agent_events = [
        Event(
            id="evt-agent-a",
            title="Agent Event A",
            url="https://luma.com/agent-a",
            start_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            guest_count=200,
            sources=["category:test"],
        ),
        Event(
            id="evt-agent-b",
            title="Agent Event B",
            url="https://luma.com/agent-b",
            start_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
            guest_count=150,
            sources=["category:test"],
        ),
    ]
    _write_cache(tmp_path, events=agent_events)
    ids = [e.id for e in agent_events]

    def _mock_query_iter(*args, **kwargs):
        yield FinalResult(result=EventListResult(ids=ids))

    with mock.patch("luma.agent.agent.Agent.query_iter", side_effect=_mock_query_iter):
        rc = _run_cli(["--cache-dir", str(tmp_path), "find events"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Agent Event A" in out
    assert "Agent Event B" in out


def test_free_text_agent_exception(tmp_path, capsys):
    from luma.agent.agent import AgentError

    def _mock_query_iter(*args, **kwargs):
        raise AgentError("boom")

    with mock.patch("luma.agent.agent.Agent.query_iter", side_effect=_mock_query_iter):
        rc = _run_cli(["--cache-dir", str(tmp_path), "hello"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --json output tests
# ---------------------------------------------------------------------------


def test_json_query(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "--min-guest", "0"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["type"] == "query"
    assert len(data["events"]) > 0
    for event in data["events"]:
        assert "title" in event
        assert "url" in event
        assert "start_at" in event
        assert "guest_count" in event


def test_json_agent_text(tmp_path, capsys):
    from luma.agent.agent import TextResult

    with mock.patch("luma.agent.agent.Agent.query", return_value=TextResult(text="hello response")):
        rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "hello"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["type"] == "text"
    assert data["text"] == "hello response"


def test_json_agent_events(tmp_path, capsys):
    from luma.agent.agent import EventListResult

    agent_events = [
        Event(
            id="evt-agent-a",
            title="Agent Event A",
            url="https://luma.com/agent-a",
            start_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            guest_count=200,
            sources=["category:test"],
        ),
    ]
    _write_cache(tmp_path, events=agent_events)
    ids = [e.id for e in agent_events]

    with mock.patch("luma.agent.agent.Agent.query", return_value=EventListResult(ids=ids)):
        rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "find events"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["type"] == "events"
    assert data["total"] == 1
    assert data["events"][0]["title"] == "Agent Event A"


def test_json_on_refresh_rejected(tmp_path, capsys):
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "refresh"])
    assert rc == 2
    assert "--json is not supported" in capsys.readouterr().err


def test_json_on_chat_rejected(tmp_path, capsys):
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "chat"])
    assert rc == 2
    assert "--json is not supported" in capsys.readouterr().err


def test_json_ignores_top(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "--top", "1", "--min-guest", "0"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["events"]) == 3


def test_json_discard(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "--discard", "--min-guest", "0"])
    assert rc == 0
    seen_path = tmp_path / "seen.json"
    assert seen_path.is_file()
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
    assert set(seen) == {e.url for e in SAMPLE_EVENTS if e.guest_count >= 0}


def test_json_no_cache_empty_stdout(tmp_path, capsys):
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No cached events" in captured.err
