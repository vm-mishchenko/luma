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
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import luma.cli as cli
from luma.models import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_start(minutes_ahead: int) -> str:
    """Return an ISO timestamp *minutes_ahead* from now, clamped to today in LA."""
    from zoneinfo import ZoneInfo
    la = ZoneInfo("America/Los_Angeles")
    now_la = datetime.now(la)
    candidate = now_la + timedelta(minutes=minutes_ahead)
    if candidate.date() != now_la.date():
        candidate = now_la.replace(hour=23, minute=59, second=0, microsecond=0)
    return candidate.astimezone(timezone.utc).isoformat()


SAMPLE_EVENTS = [
    Event(
        id="evt-test1",
        title="AI Meetup",
        url="https://luma.com/ai-meetup",
        start_at=_sample_start(10),
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
        start_at=_sample_start(20),
        guest_count=80,
        sources=["category:tech"],
    ),
    Event(
        id="evt-test3",
        title="Small Event",
        url="https://luma.com/small-event",
        start_at=_sample_start(30),
        guest_count=10,
        sources=["category:tech"],
    ),
]


def _write_cache(tmp_path, events=None):
    """Write a minimal events.json under *tmp_path*/cache/."""
    if events is None:
        events = SAMPLE_EVENTS
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "events.json"
    path.write_text(json.dumps([e.model_dump() for e in events], indent=2), encoding="utf-8")
    return path


_LLM_CONFIG_BLOCK = """\
[llm]
provider = "anthropic"

[llm.anthropic]
api_key = "sk-ant-test"
model = "claude-sonnet-4-20250514"
"""


def _write_config(tmp_path, content: str = "") -> pathlib.Path:
    """Write a TOML config string to a temp file and return the path."""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    return path


def _run_cli(argv):
    """Run CLI with given argv list, return exit code."""
    with mock.patch.object(sys, "argv", ["luma"] + list(argv)):
        try:
            return cli.main()
        except SystemExit as exc:
            return exc.code


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
    assert "Fetched 3 events" in err
    assert (tmp_path / "cache" / "events.json").is_file()


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
    seen_path = tmp_path / "preferences" / "seen.json"
    assert seen_path.is_file()
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
    assert len(seen) > 0


def test_show_all_includes_seen(tmp_path, capsys):
    _write_cache(tmp_path)

    seen_path = tmp_path / "preferences" / "seen.json"
    seen_path.parent.mkdir(parents=True, exist_ok=True)
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

    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)

    def _mock_query_iter(*args, **kwargs):
        yield FinalResult(result=TextResult(text="Here are your events."))

    with mock.patch("luma.agent.agent.Agent.query_iter", side_effect=_mock_query_iter):
        rc = _run_cli(["--cache-dir", str(tmp_path), "hello"])
    assert rc == 0
    assert "Here are your events." in capsys.readouterr().out


def test_free_text_with_flags(tmp_path, capsys):
    from luma.agent.agent import FinalResult, TextResult

    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)

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

    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)
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

    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)

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

    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)

    with mock.patch("luma.agent.agent.Agent.query", return_value=TextResult(text="hello response")):
        rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "hello"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["type"] == "text"
    assert data["text"] == "hello response"


def test_json_agent_events(tmp_path, capsys):
    from luma.agent.agent import EventListResult

    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)
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
    seen_path = tmp_path / "preferences" / "seen.json"
    assert seen_path.is_file()
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
    assert set(seen) == {e.url for e in SAMPLE_EVENTS if e.guest_count >= 0}


def test_json_no_cache_empty_stdout(tmp_path, capsys):
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "No cached events" in captured.err


# ---------------------------------------------------------------------------
# Config loading tests
# ---------------------------------------------------------------------------


def test_no_config_auto_creates(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    assert not cfg.exists()
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path)])
    assert rc == 0
    assert cfg.is_file()
    content = cfg.read_text(encoding="utf-8")
    assert "api_key" in content


def test_valid_config_loads(tmp_path, capsys):
    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path)])
    assert rc == 0


def test_malformed_toml_exits_2(tmp_path, capsys):
    cfg = _write_config(tmp_path, "[invalid toml\n")
    rc = _run_cli(["--cache-dir", str(tmp_path)])
    assert rc == 2
    assert "malformed" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# Shortcut tests
# ---------------------------------------------------------------------------


def test_sc_runs_shortcut(tmp_path, capsys):
    cfg = _write_config(
        tmp_path,
        _LLM_CONFIG_BLOCK + '[shortcuts]\npopular = ["--sort", "guest", "--min-guest", "100"]\n',
    )
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "sc", "popular"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out
    assert "Small Event" not in out


def test_sc_override_with_cli(tmp_path, capsys):
    cfg = _write_config(
        tmp_path,
        _LLM_CONFIG_BLOCK + '[shortcuts]\nby-guest = ["--sort", "guest"]\n',
    )
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "sc", "by-guest", "--top", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Top 1 events" in out


def test_sc_list_shortcuts(tmp_path, capsys):
    _write_config(
        tmp_path,
        '[shortcuts]\npopular = ["--sort", "guest"]\nweekend = ["--range", "weekend"]\n',
    )
    rc = _run_cli(["--cache-dir", str(tmp_path), "sc"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "popular" in out
    assert "weekend" in out
    assert "Add shortcuts to" in out
    assert "[shortcuts]" in out


def test_sc_unknown_name(tmp_path, capsys):
    _write_config(
        tmp_path,
        '[shortcuts]\npopular = ["--sort", "guest"]\n',
    )
    rc = _run_cli(["--cache-dir", str(tmp_path), "sc", "nonexistent"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "popular" in err


def test_sc_no_config(tmp_path, capsys):
    root = tmp_path / "new"
    cfg = root / "config.toml"
    assert not cfg.exists()
    rc = _run_cli(["--cache-dir", str(root), "sc", "foo"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "foo" in err
    assert cfg.is_file()


# ---------------------------------------------------------------------------
# Range tests
# ---------------------------------------------------------------------------


def _make_events_at_offsets(*day_offsets):
    """Create events at given day offsets from today noon LA time."""
    from zoneinfo import ZoneInfo
    la_tz = ZoneInfo("America/Los_Angeles")
    now_la = datetime.now(la_tz)
    base = now_la.replace(hour=12, minute=0, second=0, microsecond=0)
    events = []
    for i, offset in enumerate(day_offsets):
        start = (base + timedelta(days=offset)).astimezone(timezone.utc)
        events.append(
            Event(
                id=f"evt-r{i}",
                title=f"Event Day+{offset}",
                url=f"https://luma.com/evt-r{i}",
                start_at=start.isoformat(),
                guest_count=50 + i * 10,
                sources=["category:test"],
            )
        )
    return events


def test_range_today(tmp_path, capsys):
    events = _make_events_at_offsets(0, 1, 2)
    _write_cache(tmp_path, events=events)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "today"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Event Day+0" in out
    assert "Event Day+1" not in out


def test_range_tomorrow(tmp_path, capsys):
    events = _make_events_at_offsets(0, 1, 2)
    _write_cache(tmp_path, events=events)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "tomorrow"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Event Day+1" in out
    assert "Event Day+0" not in out
    assert "Event Day+2" not in out


def test_range_week(tmp_path, capsys):
    events = _make_events_at_offsets(0, 1, 2, 3, 4, 5, 6, 7, 8)
    _write_cache(tmp_path, events=events)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "week"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Event Day+0" in out
    # Events beyond this week's Sunday should not appear
    from datetime import date
    today = date.today()
    days_to_sunday = 6 - today.weekday()
    for offset in range(8, 9):
        if offset > days_to_sunday:
            assert f"Event Day+{offset}" not in out


def test_range_week_plus_1(tmp_path, capsys):
    events = _make_events_at_offsets(*range(0, 21))
    _write_cache(tmp_path, events=events)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "week+1"])
    assert rc == 0
    out = capsys.readouterr().out
    from datetime import date
    today = date.today()
    wd = today.weekday()
    days_to_next_monday = (7 - wd) % 7 or 7
    for d in range(days_to_next_monday, days_to_next_monday + 7):
        assert f"Event Day+{d}" in out
    assert "Event Day+0" not in out


def test_range_weekday(tmp_path, capsys):
    events = _make_events_at_offsets(*range(0, 10))
    _write_cache(tmp_path, events=events)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "weekday"])
    assert rc == 0


def test_range_weekend(tmp_path, capsys):
    events = _make_events_at_offsets(*range(0, 10))
    _write_cache(tmp_path, events=events)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "weekend"])
    assert rc == 0


def test_range_weekend_plus_1(tmp_path, capsys):
    events = _make_events_at_offsets(*range(0, 20))
    _write_cache(tmp_path, events=events)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "weekend+1"])
    assert rc == 0


def test_range_with_days_error(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "week", "--days", "7"])
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "cannot be used" in err


def test_range_invalid(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--range", "foobar"])
    assert rc == 2


# ---------------------------------------------------------------------------
# Like / Dislike tests
# ---------------------------------------------------------------------------


def _write_preferences(preferences_dir, filename, events):
    """Write a preference file under *preferences_dir*."""
    preferences_dir.mkdir(parents=True, exist_ok=True)
    path = preferences_dir / filename
    path.write_text(
        json.dumps([e.model_dump() for e in events], indent=2), encoding="utf-8"
    )


def test_like_saves_event(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    prefs = tmp_path / "preferences"
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 0
    liked_path = prefs / "liked.json"
    assert liked_path.is_file()
    liked = json.loads(liked_path.read_text(encoding="utf-8"))
    assert len(liked) == 1
    assert liked[0]["id"] == SAMPLE_EVENTS[0].id


def test_like_hides_already_liked(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    prefs = tmp_path / "preferences"
    _write_preferences(prefs, "liked.json", [SAMPLE_EVENTS[0]])
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 0
    liked = json.loads((prefs / "liked.json").read_text(encoding="utf-8"))
    liked_ids = [e["id"] for e in liked]
    assert SAMPLE_EVENTS[1].id in liked_ids
    assert liked_ids.count(SAMPLE_EVENTS[0].id) == 1


def test_like_hides_already_disliked(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    prefs = tmp_path / "preferences"
    _write_preferences(prefs, "disliked.json", [SAMPLE_EVENTS[0]])
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 0
    liked = json.loads((prefs / "liked.json").read_text(encoding="utf-8"))
    liked_ids = [e["id"] for e in liked]
    # First event was disliked so hidden; position 1 should be the second event
    assert SAMPLE_EVENTS[1].id in liked_ids
    assert SAMPLE_EVENTS[0].id not in liked_ids


def test_like_empty_input(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    prefs = tmp_path / "preferences"
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 0
    assert not (prefs / "liked.json").exists()


def test_like_ctrl_c(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)

    def _raise_interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise_interrupt)
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 0


def test_like_non_tty(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    monkeypatch.setattr("sys.stdin", type("FakeNoTTY", (), {"isatty": lambda self: False})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 2


def test_like_with_filters(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    prefs = tmp_path / "preferences"
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like", "--min-guest", "100"])
    assert rc == 0
    liked = json.loads((prefs / "liked.json").read_text(encoding="utf-8"))
    assert len(liked) == 1
    assert liked[0]["guest_count"] >= 100


def test_like_invalid_number(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "999")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 2


def test_inline_dislike_saves_event(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    prefs = tmp_path / "preferences"
    monkeypatch.setattr("builtins.input", lambda _: "-1")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 0
    disliked_path = prefs / "disliked.json"
    assert disliked_path.is_file()
    disliked = json.loads(disliked_path.read_text(encoding="utf-8"))
    assert len(disliked) == 1
    assert disliked[0]["id"] == SAMPLE_EVENTS[0].id


def test_inline_mixed_like_and_dislike(tmp_path, capsys, monkeypatch):
    _write_cache(tmp_path)
    prefs = tmp_path / "preferences"
    monkeypatch.setattr("builtins.input", lambda _: "1 -2")
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())
    rc = _run_cli(["--cache-dir", str(tmp_path), "like"])
    assert rc == 0
    liked = json.loads((prefs / "liked.json").read_text(encoding="utf-8"))
    disliked = json.loads((prefs / "disliked.json").read_text(encoding="utf-8"))
    assert len(liked) == 1
    assert liked[0]["id"] == SAMPLE_EVENTS[0].id
    assert len(disliked) == 1
    assert disliked[0]["id"] == SAMPLE_EVENTS[1].id


# ---------------------------------------------------------------------------
# Suggest tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Query subcommand tests
# ---------------------------------------------------------------------------


SAMPLE_EVENTS_WITH_ZERO = SAMPLE_EVENTS + [
    Event(
        id="evt-test-zero",
        title="Zero Guest Event",
        url="https://luma.com/zero-guest",
        start_at=_sample_start(40),
        guest_count=0,
        sources=["category:tech"],
    ),
]


def test_query_subcommand_with_cache(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "query", "--min-guest", "0"])
    assert rc == 0
    assert "Top" in capsys.readouterr().out


def test_query_subcommand_no_cache(tmp_path, capsys):
    rc = _run_cli(["--cache-dir", str(tmp_path), "query"])
    assert rc == 1
    assert "No cached events" in capsys.readouterr().err


def test_query_subcommand_free_text(tmp_path, capsys):
    from luma.agent.agent import FinalResult, TextResult

    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)

    def _mock_query_iter(*args, **kwargs):
        yield FinalResult(result=TextResult(text="Here are your events."))

    with mock.patch("luma.agent.agent.Agent.query_iter", side_effect=_mock_query_iter):
        rc = _run_cli(["--cache-dir", str(tmp_path), "query", "hello"])
    assert rc == 0
    assert "Here are your events." in capsys.readouterr().out


def test_query_subcommand_with_json(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--json", "query", "--min-guest", "0"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["type"] == "query"
    assert len(data["events"]) > 0


def test_query_subcommand_filters(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "query", "--city", "San Francisco"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI Meetup" in out
    assert "Tech Talk" not in out


def test_query_subcommand_discard(tmp_path, capsys):
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "query", "--discard", "--min-guest", "0"])
    assert rc == 0
    seen_path = tmp_path / "preferences" / "seen.json"
    assert seen_path.is_file()
    seen = json.loads(seen_path.read_text(encoding="utf-8"))
    assert len(seen) > 0


def test_bare_form_default_min_guest(tmp_path, capsys):
    _write_cache(tmp_path, events=SAMPLE_EVENTS_WITH_ZERO)
    rc = _run_cli(["--cache-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Zero Guest Event" not in out


def test_bare_form_min_guest_override(tmp_path, capsys):
    _write_cache(tmp_path, events=SAMPLE_EVENTS_WITH_ZERO)
    rc = _run_cli(["--cache-dir", str(tmp_path), "--min-guest", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Zero Guest Event" in out


def test_query_subcommand_no_default_min_guest(tmp_path, capsys):
    _write_cache(tmp_path, events=SAMPLE_EVENTS_WITH_ZERO)
    rc = _run_cli(["--cache-dir", str(tmp_path), "query"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Zero Guest Event" in out


# ---------------------------------------------------------------------------
# Suggest tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# LLM key optional tests
# ---------------------------------------------------------------------------

_NO_LLM_CONFIG = ""

_EMPTY_PROVIDER_CONFIG = """\
[llm]
provider = "anthropic"

[llm.anthropic]
"""


def test_refresh_no_llm_section(tmp_path, capsys):
    cfg = _write_config(tmp_path, _NO_LLM_CONFIG)
    with mock.patch("luma.refresh.download_events", return_value=SAMPLE_EVENTS):
        rc = _run_cli(["--cache-dir", str(tmp_path), "refresh"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Skipping LLM enrichment" in err
    assert (tmp_path / "cache" / "events.json").is_file()


def test_refresh_empty_provider_block(tmp_path, capsys):
    cfg = _write_config(tmp_path, _EMPTY_PROVIDER_CONFIG)
    with mock.patch("luma.refresh.download_events", return_value=SAMPLE_EVENTS):
        rc = _run_cli(["--cache-dir", str(tmp_path), "refresh"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Skipping LLM enrichment" in err


def test_refresh_valid_key_runs_enrichment(tmp_path, capsys):
    cfg = _write_config(tmp_path, _LLM_CONFIG_BLOCK)
    with mock.patch("luma.refresh.download_events", return_value=SAMPLE_EVENTS):
        rc = _run_cli(["--cache-dir", str(tmp_path), "refresh"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Skipping LLM enrichment" not in err


def test_chat_no_key_fails(tmp_path, capsys):
    cfg = _write_config(tmp_path, _NO_LLM_CONFIG)
    rc = _run_cli(["--cache-dir", str(tmp_path), "chat"])
    assert rc == 2
    assert "Error" in capsys.readouterr().err


def test_suggest_no_key_fails(tmp_path, capsys):
    _write_config(tmp_path, _NO_LLM_CONFIG)
    _write_cache(tmp_path)
    _write_preferences(tmp_path / "preferences", "liked.json", [SAMPLE_EVENTS[0]])
    rc = _run_cli(["--cache-dir", str(tmp_path), "suggest"])
    assert rc == 2
    assert "Error" in capsys.readouterr().err


def test_query_structured_no_key_works(tmp_path, capsys):
    cfg = _write_config(tmp_path, _NO_LLM_CONFIG)
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "query", "--min-guest", "0"])
    assert rc == 0
    assert "Top" in capsys.readouterr().out


def test_query_free_text_no_key_fails(tmp_path, capsys):
    cfg = _write_config(tmp_path, _NO_LLM_CONFIG)
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "query", "hello"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "LLM API key required" in err
    assert str(tmp_path / "config.toml") in err


def test_config_template_commented_out(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    assert not cfg.exists()
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path)])
    assert rc == 0
    content = cfg.read_text(encoding="utf-8")
    assert '# api_key = ""' in content
    assert "# model = " in content
    assert 'api_key = "sk-ant-..."' not in content


def test_refresh_skip_message_shows_config_path(tmp_path, capsys):
    cfg = _write_config(tmp_path, _NO_LLM_CONFIG)
    with mock.patch("luma.refresh.download_events", return_value=SAMPLE_EVENTS):
        rc = _run_cli(["--cache-dir", str(tmp_path), "refresh"])
    assert rc == 0
    err = capsys.readouterr().err
    assert str(cfg) in err


# ---------------------------------------------------------------------------
# Suggest tests
# ---------------------------------------------------------------------------


def test_suggest_no_cache(tmp_path, capsys):
    _write_config(tmp_path, _LLM_CONFIG_BLOCK)
    rc = _run_cli(["--cache-dir", str(tmp_path), "suggest"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "luma refresh" in err
    assert "luma like" in err


def test_suggest_no_likes(tmp_path, capsys):
    _write_config(tmp_path, _LLM_CONFIG_BLOCK)
    _write_cache(tmp_path)
    rc = _run_cli(["--cache-dir", str(tmp_path), "suggest"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "luma like" in err


def test_suggest_success(tmp_path, capsys, monkeypatch):
    from luma.agent import EventListResult, FinalResult

    _write_config(tmp_path, _LLM_CONFIG_BLOCK)
    _write_cache(tmp_path)
    _write_preferences(tmp_path / "preferences", "liked.json", [SAMPLE_EVENTS[0]])

    def _fake_query_iter(self, text, *, loader=None):
        yield FinalResult(result=EventListResult(ids=[SAMPLE_EVENTS[1].id]))

    monkeypatch.setattr("luma.agent.Agent.query_iter", _fake_query_iter)
    rc = _run_cli(["--cache-dir", str(tmp_path), "suggest"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tech Talk" in out


def test_suggest_agent_error(tmp_path, capsys, monkeypatch):
    from luma.agent import AgentError

    _write_config(tmp_path, _LLM_CONFIG_BLOCK)
    _write_cache(tmp_path)
    _write_preferences(tmp_path / "preferences", "liked.json", [SAMPLE_EVENTS[0]])

    def _raise_error(self, text, *, loader=None):
        raise AgentError("API failed")

    monkeypatch.setattr("luma.agent.Agent.query_iter", _raise_error)
    rc = _run_cli(["--cache-dir", str(tmp_path), "suggest"])
    assert rc == 1
    assert "API failed" in capsys.readouterr().err
