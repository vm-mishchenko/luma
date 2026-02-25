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

import cli
import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_EVENTS = [
    {
        "title": "AI Meetup",
        "url": "https://luma.com/ai-meetup",
        "start_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "guest_count": 120,
        "sources": ["category:ai"],
    },
    {
        "title": "Tech Talk",
        "url": "https://luma.com/tech-talk",
        "start_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "guest_count": 80,
        "sources": ["category:tech"],
    },
    {
        "title": "Small Event",
        "url": "https://luma.com/small-event",
        "start_at": (datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        "guest_count": 10,
        "sources": ["category:tech"],
    },
]


def _write_cache(tmp_path, events=None, fetched_at=None):
    """Write a minimal cache file directly into *tmp_path*."""
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)
    if events is None:
        events = SAMPLE_EVENTS
    stamp = fetched_at.strftime("%Y-%m-%d_%H-%M-%S")
    path = tmp_path / f"events-{stamp}.json"
    payload = {"fetched_at": fetched_at.isoformat(), "events": events}
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
    with mock.patch("refresh.download_events", return_value=SAMPLE_EVENTS):
        rc = _run_cli(["--cache-dir", str(tmp_path), "refresh"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Cached 3 events" in err

    cache_files = list(tmp_path.glob("events-*.json"))
    assert len(cache_files) == 1


def test_refresh_network_error(tmp_path, capsys):
    with mock.patch("refresh.download_events", side_effect=OSError("net down")):
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
    with mock.patch("refresh.download_events", return_value=[]) as m:
        _run_cli(["--cache-dir", str(tmp_path), "refresh", "--retries", "3"])
    assert m.call_args.kwargs["retries"] == 3


def test_retries_on_main_parser_rejected():
    rc = _run_cli(["--retries", "3"])
    assert rc == 2


def test_days_on_refresh_rejected():
    rc = _run_cli(["refresh", "--days", "7"])
    assert rc == 2


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
