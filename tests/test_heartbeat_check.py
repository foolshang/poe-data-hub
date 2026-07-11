"""Unit tests for tools/heartbeat_check.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from tools import heartbeat_check


def test_check_prices_freshness_ok_when_recent():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_prices_freshness(generated_at, now=now) is None


def test_check_prices_freshness_flags_when_stale():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    problem = heartbeat_check.check_prices_freshness(generated_at, now=now)
    assert problem is not None
    assert "60 minutes old" in problem


def test_check_prices_freshness_boundary_not_flagged():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=44)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_prices_freshness(generated_at, now=now) is None


def test_check_meta_freshness_ok_when_recent():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    last_meta = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_meta_freshness(last_meta, now=now) is None


def test_check_meta_freshness_flags_when_stale():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    last_meta = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    problem = heartbeat_check.check_meta_freshness(last_meta, now=now)
    assert problem is not None
    assert "30.0 hours old" in problem


def test_run_returns_0_when_both_fresh():
    now = datetime.now(timezone.utc)
    prices_resp = MagicMock()
    prices_resp.json.return_value = {"generated_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = MagicMock()
    status_resp.json.return_value = {"last_meta": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}

    with patch("requests.get", side_effect=[prices_resp, status_resp]):
        assert heartbeat_check.run() == 0


def test_run_returns_1_when_prices_stale():
    now = datetime.now(timezone.utc)
    prices_resp = MagicMock()
    prices_resp.json.return_value = {"generated_at": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = MagicMock()
    status_resp.json.return_value = {"last_meta": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}

    with patch("requests.get", side_effect=[prices_resp, status_resp]):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_meta_stale():
    now = datetime.now(timezone.utc)
    prices_resp = MagicMock()
    prices_resp.json.return_value = {"generated_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = MagicMock()
    status_resp.json.return_value = {"last_meta": (now - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")}

    with patch("requests.get", side_effect=[prices_resp, status_resp]):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_fetch_fails():
    with patch("requests.get", side_effect=RuntimeError("network blip")):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_response_missing_expected_key():
    prices_resp = MagicMock()
    prices_resp.json.return_value = {"not_generated_at": "oops"}
    status_resp = MagicMock()
    status_resp.json.return_value = {"last_meta": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    with patch("requests.get", side_effect=[prices_resp, status_resp]):
        assert heartbeat_check.run() == 1
