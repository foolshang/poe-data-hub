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


def test_check_news_freshness_ok_when_recent():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_news_freshness(generated_at, now=now) is None


def test_check_news_freshness_flags_when_stale():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    problem = heartbeat_check.check_news_freshness(generated_at, now=now)
    assert problem is not None
    assert "20 minutes old" in problem


def test_check_news_freshness_boundary_not_flagged():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_news_freshness(generated_at, now=now) is None


def test_check_hardcore_freshness_ok_when_recent():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_hardcore_freshness(generated_at, now=now) is None


def test_check_hardcore_freshness_flags_when_stale():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    problem = heartbeat_check.check_hardcore_freshness(generated_at, now=now)
    assert problem is not None
    assert "hardcore/latest.json" in problem


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


def test_check_per_category_health_ok_when_all_ok():
    per_category = {"Currency": {"ok": True, "count": 5}, "Fragment": {"ok": True, "count": 2}}
    assert heartbeat_check.check_per_category_health("ninja", per_category) is None


def test_check_per_category_health_ok_when_empty():
    assert heartbeat_check.check_per_category_health("ninja", {}) is None


def test_check_per_category_health_flags_when_more_than_half_failing():
    per_category = {
        "Currency": {"ok": False, "count": 0},
        "Fragment": {"ok": False, "count": 0},
        "Rune": {"ok": False, "count": 0},
        "Essence": {"ok": True, "count": 5},
    }
    problem = heartbeat_check.check_per_category_health("ninja", per_category)
    assert problem is not None
    assert "3/4" in problem


def test_check_per_category_health_not_flagged_at_exactly_half():
    per_category = {
        "Currency": {"ok": False, "count": 0},
        "Fragment": {"ok": False, "count": 0},
        "Rune": {"ok": True, "count": 5},
        "Essence": {"ok": True, "count": 5},
    }
    assert heartbeat_check.check_per_category_health("ninja", per_category) is None


def _fresh_prices_resp(now, per_category_by_source=None):
    resp = MagicMock()
    payload = {"generated_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if per_category_by_source is not None:
        payload["sample"] = {"per_category": per_category_by_source}
    resp.json.return_value = payload
    return resp


def _fresh_news_resp(now):
    resp = MagicMock()
    resp.json.return_value = {"generated_at": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return resp


def _index_resp(hardcore=None):
    resp = MagicMock()
    resp.json.return_value = {"hardcore": hardcore}
    return resp


def _status_resp(now, poe2_age=timedelta(hours=2), poe1_age=timedelta(hours=2)):
    resp = MagicMock()
    resp.json.return_value = {
        "meta": {
            "poe2": {"last_meta": (now - poe2_age).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "poe1": {"last_meta": (now - poe1_age).strftime("%Y-%m-%dT%H:%M:%SZ")},
        }
    }
    return resp


# call order inside run(): poe2 prices, poe1 prices, poe2 news, poe1 news,
# poe2 index, poe1 index, status (PRICES_URLS, NEWS_URLS, INDEX_URLS dict
# order, then status)
def _fresh_sequence(now, count=2):
    return (
        [_fresh_prices_resp(now) for _ in range(count)]
        + [_fresh_news_resp(now), _fresh_news_resp(now)]
        + [_index_resp(), _index_resp()]
    )


def test_run_returns_0_when_both_fresh():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now)

    with patch("requests.get", side_effect=_fresh_sequence(now) + [status_resp]):
        assert heartbeat_check.run() == 0


def test_run_returns_1_when_poe2_prices_stale():
    now = datetime.now(timezone.utc)
    stale_resp = MagicMock()
    stale_resp.json.return_value = {"generated_at": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = _status_resp(now)

    sequence = [stale_resp, _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_prices_stale():
    now = datetime.now(timezone.utc)
    stale_resp = MagicMock()
    stale_resp.json.return_value = {"generated_at": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = _status_resp(now)

    sequence = [_fresh_prices_resp(now), stale_resp, _fresh_news_resp(now), _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe2_news_stale():
    now = datetime.now(timezone.utc)
    stale_news = MagicMock()
    stale_news.json.return_value = {"generated_at": (now - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = _status_resp(now)

    sequence = [_fresh_prices_resp(now), _fresh_prices_resp(now), stale_news, _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_news_stale():
    now = datetime.now(timezone.utc)
    stale_news = MagicMock()
    stale_news.json.return_value = {"generated_at": (now - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = _status_resp(now)

    sequence = [_fresh_prices_resp(now), _fresh_prices_resp(now), _fresh_news_resp(now), stale_news, _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe2_meta_stale():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now, poe2_age=timedelta(hours=40))

    with patch("requests.get", side_effect=_fresh_sequence(now) + [status_resp]):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_meta_stale():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now, poe1_age=timedelta(hours=40))

    with patch("requests.get", side_effect=_fresh_sequence(now) + [status_resp]):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe2_ninja_categories_mostly_failing():
    now = datetime.now(timezone.utc)
    degraded = _fresh_prices_resp(now, per_category_by_source={
        "ninja": {
            "Currency": {"ok": False, "count": 0},
            "Fragment": {"ok": False, "count": 0},
            "Rune": {"ok": True, "count": 5},
        },
    })
    status_resp = _status_resp(now)

    sequence = [degraded, _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_0_when_categories_all_ok():
    now = datetime.now(timezone.utc)
    healthy = _fresh_prices_resp(now, per_category_by_source={"ninja": {"Currency": {"ok": True, "count": 5}}})
    status_resp = _status_resp(now)

    sequence = [healthy, _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 0


def test_run_returns_1_when_hardcore_stale():
    now = datetime.now(timezone.utc)
    stale_hardcore = _index_resp(hardcore={"league": "HC Runes of Aldur", "path": "poe2/prices/hardcore/latest.json", "generated_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    status_resp = _status_resp(now)

    sequence = [_fresh_prices_resp(now), _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), stale_hardcore, _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_0_when_hardcore_null():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now)

    with patch("requests.get", side_effect=_fresh_sequence(now) + [status_resp]):
        assert heartbeat_check.run() == 0


def test_run_returns_0_when_hardcore_fresh():
    now = datetime.now(timezone.utc)
    fresh_hardcore = _index_resp(hardcore={"league": "HC Runes of Aldur", "path": "poe2/prices/hardcore/latest.json", "generated_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    status_resp = _status_resp(now)

    sequence = [_fresh_prices_resp(now), _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), fresh_hardcore, _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 0


def test_run_returns_1_when_fetch_fails():
    with patch("requests.get", side_effect=RuntimeError("network blip")):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_response_missing_expected_key():
    now = datetime.now(timezone.utc)
    bad_resp = MagicMock()
    bad_resp.json.return_value = {"not_generated_at": "oops"}
    status_resp = _status_resp(now)

    sequence = [bad_resp, _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1
