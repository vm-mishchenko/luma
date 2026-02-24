"""Tests for CLI subcommand split: luma refresh / luma (query)."""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from luma import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache(
    tmp_path: pathlib.Path,
    events: list[dict] | None = None,
    fetched_at: datetime | None = None,
) -> pathlib.Path:
    """Write a minimal cache file and point CACHE_DIR at *tmp_path*."""
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)
    if events is None:
        events = [
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
    stamp = fetched_at.strftime("%Y-%m-%d_%H-%M-%S")
    path = tmp_path / f"events-{stamp}.json"
    payload = {"fetched_at": fetched_at.isoformat(), "events": events}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Point CACHE_DIR and SEEN_FILE at a temp directory for every test."""
    monkeypatch.setattr(cli, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cli, "SEEN_FILE", tmp_path / "seen.json")


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_default_is_query(self):
        args = cli.parse_args([])
        assert args.command is None

    def test_refresh_subcommand(self):
        args = cli.parse_args(["refresh"])
        assert args.command == "refresh"
        assert args.retries == 5

    def test_refresh_with_retries(self):
        args = cli.parse_args(["refresh", "--retries", "3"])
        assert args.command == "refresh"
        assert args.retries == 3

    def test_query_days(self):
        args = cli.parse_args(["--days", "7"])
        assert args.command is None
        assert args.days == 7

    def test_refresh_flag_removed(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["--refresh"])

    def test_retries_not_on_main_parser(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["--retries", "3"])

    def test_days_not_on_refresh(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["refresh", "--days", "7"])


# ---------------------------------------------------------------------------
# find_latest_cache
# ---------------------------------------------------------------------------

class TestFindLatestCache:
    def test_returns_none_when_no_cache(self, tmp_path):
        assert cli.find_latest_cache() is None

    def test_returns_newest_file(self, tmp_path):
        old = tmp_path / "events-2026-01-01_00-00-00.json"
        old.write_text("{}", encoding="utf-8")
        new = tmp_path / "events-2026-02-01_00-00-00.json"
        new.write_text("{}", encoding="utf-8")
        result = cli.find_latest_cache()
        assert result is not None
        assert result.name == new.name

    def test_returns_stale_cache_too(self, tmp_path):
        """Unlike the old find_fresh_cache, stale caches are still returned."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=100)
        _make_cache(tmp_path, fetched_at=old_time)
        result = cli.find_latest_cache()
        assert result is not None


# ---------------------------------------------------------------------------
# cmd_refresh
# ---------------------------------------------------------------------------

class TestCmdRefresh:
    def test_success(self, tmp_path, capsys):
        cache_path = tmp_path / "events-2026-02-23_00-00-00.json"
        with mock.patch.object(cli, "refresh", return_value=(1, cache_path)):
            rc = cli.cmd_refresh(retries=5)
        assert rc == 0
        assert f"Cached 1 events to {cache_path}" in capsys.readouterr().err

    def test_network_error_returns_1(self, tmp_path, capsys):
        with mock.patch.object(cli, "refresh", side_effect=OSError("network down")):
            rc = cli.cmd_refresh(retries=5)
        assert rc == 1
        assert "Error fetching events: network down" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_query
# ---------------------------------------------------------------------------

class TestCmdQuery:
    def test_no_cache_exits_1(self, tmp_path, capsys):
        args = cli.parse_args([])
        rc = cli.cmd_query(args)
        assert rc == 1
        assert "No cached events" in capsys.readouterr().err

    def test_loads_cache_and_displays(self, tmp_path, capsys):
        _make_cache(tmp_path)
        args = cli.parse_args(["--min-guest", "0"])
        rc = cli.cmd_query(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Top" in out

    def test_stale_cache_warns(self, tmp_path, capsys):
        stale_time = datetime.now(timezone.utc) - timedelta(days=3)
        _make_cache(tmp_path, fetched_at=stale_time)
        args = cli.parse_args(["--min-guest", "0"])
        rc = cli.cmd_query(args)
        assert rc == 0
        err = capsys.readouterr().err
        assert "Warning: cache is" in err
        assert "luma refresh" in err

    def test_days_filter(self, tmp_path, capsys):
        _make_cache(tmp_path)
        args = cli.parse_args(["--days", "7", "--min-guest", "0"])
        rc = cli.cmd_query(args)
        assert rc == 0


# ---------------------------------------------------------------------------
# main dispatch
# ---------------------------------------------------------------------------

class TestMain:
    def test_dispatch_refresh(self, tmp_path):
        with mock.patch.object(cli, "cmd_refresh", return_value=0) as m:
            with mock.patch.object(sys, "argv", ["luma", "refresh"]):
                rc = cli.main()
        assert rc == 0
        m.assert_called_once_with(5)

    def test_dispatch_refresh_retries(self, tmp_path):
        with mock.patch.object(cli, "cmd_refresh", return_value=0) as m:
            with mock.patch.object(sys, "argv", ["luma", "refresh", "--retries", "3"]):
                rc = cli.main()
        assert rc == 0
        m.assert_called_once_with(3)

    def test_dispatch_query(self, tmp_path):
        _make_cache(tmp_path)
        with mock.patch.object(sys, "argv", ["luma", "--min-guest", "0"]):
            rc = cli.main()
        assert rc == 0
