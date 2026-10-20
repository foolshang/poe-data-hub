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
    generated_at = (now - timedelta(minutes=3000)).strftime("%Y-%m-%dT%H:%M:%SZ")
    problem = heartbeat_check.check_prices_freshness(generated_at, now=now)
    assert problem is not None
    assert "3000 minutes old" in problem


def test_check_prices_freshness_boundary_not_flagged():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=2879)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    generated_at = (now - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def test_check_meta_sample_total_ok_when_nonzero():
    assert heartbeat_check.check_meta_sample_total({"sample_total": 42}) is None


def test_check_meta_sample_total_flags_when_zero():
    problem = heartbeat_check.check_meta_sample_total({"sample_total": 0})
    assert problem is not None
    assert "sample_total" in problem


def test_check_meta_sample_total_flags_when_missing():
    problem = heartbeat_check.check_meta_sample_total({})
    assert problem is not None


def test_check_meta_sample_total_flags_when_summary_is_none():
    """Payloads published before this check shipped won't have
    meta_job_summary at all -- must not crash on None."""
    problem = heartbeat_check.check_meta_sample_total(None)
    assert problem is not None


def test_check_skills_index_freshness_ok_when_recent():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_skills_index_freshness(generated_at, now=now) is None


def test_check_skills_index_freshness_flags_when_stale():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(hours=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    problem = heartbeat_check.check_skills_index_freshness(generated_at, now=now)
    assert problem is not None
    assert "31.0 hours old" in problem


def test_check_skills_index_freshness_boundary_not_flagged():
    now = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(hours=29, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert heartbeat_check.check_skills_index_freshness(generated_at, now=now) is None


def test_check_skills_index_population_ok_when_nonzero():
    assert heartbeat_check.check_skills_index_population(124408) is None


def test_check_skills_index_population_flags_when_zero():
    problem = heartbeat_check.check_skills_index_population(0)
    assert problem is not None
    assert "population_total" in problem


def test_check_skills_index_population_flags_when_missing():
    problem = heartbeat_check.check_skills_index_population(None)
    assert problem is not None


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


def _fresh_skills_resp(now, population_total=100000):
    resp = MagicMock()
    resp.json.return_value = {
        "generated_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "population_total": population_total,
    }
    return resp


def _status_resp(now, poe2_age=timedelta(hours=2), poe1_age=timedelta(hours=2), poe2_sample_total=10, poe1_sample_total=10):
    resp = MagicMock()
    resp.json.return_value = {
        "meta": {
            "poe2": {
                "last_meta": (now - poe2_age).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "meta_job_summary": {"sample_total": poe2_sample_total},
            },
            "poe1": {
                "last_meta": (now - poe1_age).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "meta_job_summary": {"sample_total": poe1_sample_total},
            },
        }
    }
    return resp


def _json_resp(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    return resp


def _stats_cache(now, age_days=1.0, entries=8000):
    return {"fetched_at": (now - timedelta(days=age_days)).timestamp(),
            "data": {"result": [{"id": "explicit", "entries": [{"id": f"explicit.stat_{i}", "text": "x"} for i in range(entries)]}]}}


def _gems(now, age_days=1.0, n=900):
    return {"schema_version": 1, "generated_at": (now - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ"), "gems": [{}] * n}


def _aliases(now, game, age_days=1.0, n=1900, schema_version=1):
    return {"schema_version": schema_version, "game": game, "generated_at": (now - timedelta(days=age_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "aliases": [{}] * n, "stat_groups": [], "stat_aliases": {}}


def _weekly_fresh_responses(now, stats=None, gems=None, aliases=None):
    """call order in run(): stats caches (poe2, poe1), gems (poe2, poe1), aliases (poe2, poe1)"""
    stats = stats or [_stats_cache(now), _stats_cache(now)]
    gems = gems or [_gems(now), _gems(now)]
    aliases = aliases or [_aliases(now, "poe2", n=130), _aliases(now, "poe1")]
    return [_json_resp(x) for x in (*stats, *gems, *aliases)]


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

    sequence = _fresh_sequence(now) + [status_resp, _fresh_skills_resp(now), _fresh_skills_resp(now)] + _weekly_fresh_responses(now)
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 0


def test_run_returns_1_when_poe2_prices_stale():
    now = datetime.now(timezone.utc)
    stale_resp = MagicMock()
    stale_resp.json.return_value = {"generated_at": (now - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    status_resp = _status_resp(now)

    sequence = [stale_resp, _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_prices_stale():
    now = datetime.now(timezone.utc)
    stale_resp = MagicMock()
    stale_resp.json.return_value = {"generated_at": (now - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%SZ")}
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


def test_run_returns_1_when_poe2_meta_sample_total_zero():
    """2026-09-17: catches exactly the shape of the 2026-08-04 ninja_builds
    schema break -- last_meta stays fresh (job "succeeds") every cycle while
    the actual character sample is silently empty. Freshness alone missed
    it for ~43 days; this reads the population count directly."""
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now, poe2_sample_total=0)

    with patch("requests.get", side_effect=_fresh_sequence(now) + [status_resp]):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_meta_sample_total_zero():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now, poe1_sample_total=0)

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

    sequence = [healthy, _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), _index_resp(), _index_resp(), status_resp, _fresh_skills_resp(now), _fresh_skills_resp(now)] + _weekly_fresh_responses(now)
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 0


def test_run_returns_1_when_hardcore_stale():
    now = datetime.now(timezone.utc)
    stale_hardcore = _index_resp(hardcore={"league": "HC Runes of Aldur", "path": "poe2/prices/hardcore/latest.json", "generated_at": (now - timedelta(hours=50)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    status_resp = _status_resp(now)

    sequence = [_fresh_prices_resp(now), _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), stale_hardcore, _index_resp(), status_resp]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_0_when_hardcore_null():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now)

    sequence = _fresh_sequence(now) + [status_resp, _fresh_skills_resp(now), _fresh_skills_resp(now)] + _weekly_fresh_responses(now)
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 0


def test_run_returns_0_when_hardcore_fresh():
    now = datetime.now(timezone.utc)
    fresh_hardcore = _index_resp(hardcore={"league": "HC Runes of Aldur", "path": "poe2/prices/hardcore/latest.json", "generated_at": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    status_resp = _status_resp(now)

    sequence = [_fresh_prices_resp(now), _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now), fresh_hardcore, _index_resp(), status_resp, _fresh_skills_resp(now), _fresh_skills_resp(now)] + _weekly_fresh_responses(now)
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


def test_run_returns_1_when_poe2_skills_index_stale():
    now = datetime.now(timezone.utc)
    stale_skills = MagicMock()
    stale_skills.json.return_value = {"generated_at": (now - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ"), "population_total": 100000}
    status_resp = _status_resp(now)

    sequence = _fresh_sequence(now) + [status_resp, stale_skills, _fresh_skills_resp(now)]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_skills_index_stale():
    now = datetime.now(timezone.utc)
    stale_skills = MagicMock()
    stale_skills.json.return_value = {"generated_at": (now - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ"), "population_total": 100000}
    status_resp = _status_resp(now)

    sequence = _fresh_sequence(now) + [status_resp, _fresh_skills_resp(now), stale_skills]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe2_skills_index_population_zero():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now)

    sequence = _fresh_sequence(now) + [status_resp, _fresh_skills_resp(now, population_total=0), _fresh_skills_resp(now)]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_skills_index_population_zero():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now)

    sequence = _fresh_sequence(now) + [status_resp, _fresh_skills_resp(now), _fresh_skills_resp(now, population_total=0)]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_skills_index_fetch_fails():
    now = datetime.now(timezone.utc)
    status_resp = _status_resp(now)

    sequence = _fresh_sequence(now) + [status_resp, RuntimeError("network blip"), _fresh_skills_resp(now)]
    with patch("requests.get", side_effect=sequence):
        assert heartbeat_check.run() == 1


# --- weekly-refresh artefacts: stats cache, gems, aliases, stats_cache_stale (2026-10-20) -----------------------------

def _clean_sequence(now, **weekly):
    return _fresh_sequence(now) + [_status_resp(now), _fresh_skills_resp(now), _fresh_skills_resp(now)] + _weekly_fresh_responses(now, **weekly)


def test_check_stats_cache_ok_when_fresh_and_full():
    now = datetime.now(timezone.utc)
    assert heartbeat_check.check_stats_cache("poe1", _stats_cache(now), now) == []


def test_check_stats_cache_flags_age_over_nine_days_but_not_a_normal_late_weekly_run():
    now = datetime.now(timezone.utc)
    assert heartbeat_check.check_stats_cache("poe1", _stats_cache(now, age_days=7.4), now) == []     # weekly + 7 h of GitHub lateness
    assert heartbeat_check.check_stats_cache("poe1", _stats_cache(now, age_days=9.5), now)


def test_check_stats_cache_flags_a_fresh_but_empty_or_misshapen_upload():
    now = datetime.now(timezone.utc)
    assert heartbeat_check.check_stats_cache("poe2", _stats_cache(now, entries=10), now)           # fresh, almost empty
    assert heartbeat_check.check_stats_cache("poe2", {"fetched_at": now.timestamp(), "data": {"result": "nope"}}, now)
    assert heartbeat_check.check_stats_cache("poe2", ["not", "a", "dict"], now)
    assert heartbeat_check.check_stats_cache("poe2", {"data": {"result": []}}, now)                # no fetched_at (and no entries)


def test_check_gems_flags_age_schema_and_emptiness():
    now = datetime.now(timezone.utc)
    assert heartbeat_check.check_gems("poe1", _gems(now), now) == []
    assert heartbeat_check.check_gems("poe1", _gems(now, age_days=10), now)
    assert heartbeat_check.check_gems("poe1", _gems(now, n=5), now)
    assert heartbeat_check.check_gems("poe1", {**_gems(now), "schema_version": 2}, now)
    assert heartbeat_check.check_gems("poe1", [], now)


def test_check_aliases_poe1_needs_many_poe2_may_have_few():
    now = datetime.now(timezone.utc)
    assert heartbeat_check.check_aliases("poe1", _aliases(now, "poe1"), now) == []
    assert heartbeat_check.check_aliases("poe2", _aliases(now, "poe2", n=0), now) == []            # RePoE links few PoE2 stats: not an error
    assert heartbeat_check.check_aliases("poe1", _aliases(now, "poe1", n=0), now)                  # PoE1 with no aliases is a silent break


def test_check_aliases_flags_schema_game_age_and_missing_members():
    now = datetime.now(timezone.utc)
    assert heartbeat_check.check_aliases("poe1", _aliases(now, "poe1", schema_version=2), now)
    assert heartbeat_check.check_aliases("poe1", _aliases(now, "poe2"), now)                       # wrong game in the file
    assert heartbeat_check.check_aliases("poe1", _aliases(now, "poe1", age_days=12), now)
    broken = _aliases(now, "poe1"); del broken["stat_groups"]
    assert heartbeat_check.check_aliases("poe1", broken, now)


def test_run_returns_0_when_the_weekly_files_are_fresh():
    now = datetime.now(timezone.utc)
    with patch("requests.get", side_effect=_clean_sequence(now)):
        assert heartbeat_check.run() == 0


def test_run_returns_1_when_a_stats_cache_is_stale():
    now = datetime.now(timezone.utc)
    stale = [_stats_cache(now), _stats_cache(now, age_days=20)]
    with patch("requests.get", side_effect=_clean_sequence(now, stats=stale)):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_poe1_aliases_are_empty():
    now = datetime.now(timezone.utc)
    with patch("requests.get", side_effect=_clean_sequence(now, aliases=[_aliases(now, "poe2", n=130), _aliases(now, "poe1", n=0)])):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_the_hub_reports_its_own_stats_cache_stale():
    now = datetime.now(timezone.utc)
    status = _status_resp(now)
    status.json.return_value["stats_cache_stale"] = True
    seq = _fresh_sequence(now) + [status, _fresh_skills_resp(now), _fresh_skills_resp(now)] + _weekly_fresh_responses(now)
    with patch("requests.get", side_effect=seq):
        assert heartbeat_check.run() == 1


def test_run_returns_1_when_a_weekly_file_cannot_be_fetched():
    now = datetime.now(timezone.utc)
    seq = _fresh_sequence(now) + [_status_resp(now), _fresh_skills_resp(now), _fresh_skills_resp(now), RuntimeError("404")] + _weekly_fresh_responses(now)[1:]
    with patch("requests.get", side_effect=seq):
        assert heartbeat_check.run() == 1
