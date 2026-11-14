"""Layer 2 of the league gap (tools/heartbeat_check.py): a game between two challenge leagues is skipped for prices /
meta / skills while the gap is at most 7 days old, red after that, absent key = the old strict behaviour, news and the
weekly files are never excused, and an empty sample is tolerated for 48 h after the league's first snapshot.

Shipped separately from the hub half (tests/test_league_gap.py): the heartbeat must only go live once the hub writes
`status.json["league_gap"]`, otherwise the next gap is red exactly as before.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.processing import league_gap
from tests.test_heartbeat_check import (
    _fresh_news_resp,
    _fresh_prices_resp,
    _fresh_skills_resp,
    _index_resp,
    _json_resp,
    _weekly_fresh_responses,
)
from tools import heartbeat_check

NOW = datetime(2026, 11, 14, 19, 0, 0, tzinfo=timezone.utc)


def _ts(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_config_yaml_and_the_independent_heartbeat_copy_agree():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "config.yaml").read_text(encoding="utf-8"))
    settings = league_gap.GapSettings.from_cfg(cfg)
    assert settings.max_days == heartbeat_check.LEAGUE_GAP_MAX_DAYS == 7
    assert settings.launch_grace_hours == heartbeat_check.LAUNCH_GRACE_HOURS == 48


# ── Layer 2: heartbeat_check ───────────────────────────────────────────────────────────────────

def test_check_league_gaps_classifies_tolerated_exceeded_and_unreadable():
    status = {"league_gap": {
        "poe1": {"since": _ts(timedelta(days=3)), "last_league": "Allflame"},
        "poe2": {"since": _ts(timedelta(days=8)), "last_league": "Runes of Aldur"},
        "bad": {"since": "nonsense"}, "worse": "x",
    }}
    tolerated, problems, notices = heartbeat_check.check_league_gaps(status, now=NOW)
    assert tolerated == {"poe1"}
    assert len(problems) == 1 and "poe2: no active league for 8.0 days (> 7)" in problems[0]
    assert len(notices) == 1 and "poe1: between leagues for 3.0 days" in notices[0]


def test_check_league_gaps_missing_key_means_no_gap():
    assert heartbeat_check.check_league_gaps({}, now=NOW) == (frozenset(), [], [])
    assert heartbeat_check.check_league_gaps({"league_gap": None}, now=NOW) == (frozenset(), [], [])


def _hb_status(now, gap=None, poe1_meta_age=timedelta(days=4), poe1_sample=0, first_snapshot=None):
    meta1 = {"last_meta": (now - poe1_meta_age).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "meta_job_summary": {"sample_total": poe1_sample}}
    if first_snapshot:
        meta1["meta_job_summary"]["league_first_snapshot_at"] = first_snapshot
    payload = {"meta": {"poe2": {"last_meta": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "meta_job_summary": {"sample_total": 10}}, "poe1": meta1}}
    if gap is not None:
        payload["league_gap"] = gap
    return _json_resp(payload)


def _stale_prices(now):
    return _json_resp({"generated_at": (now - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ")})


def _stale_skills(now):
    return _json_resp({"generated_at": (now - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%SZ"), "population_total": 0})


def _poe1_in_gap_sequence(now, status, *, poe1_news=None, poe2_prices=None):
    """call order in run(): prices poe2, poe1; news poe2, poe1; index poe2, poe1; status; skills poe2, poe1; weekly"""
    return [poe2_prices or _fresh_prices_resp(now), _stale_prices(now),
            _fresh_news_resp(now), poe1_news or _fresh_news_resp(now),
            _index_resp(), _index_resp(), status, _fresh_skills_resp(now), _stale_skills(now)] + _weekly_fresh_responses(now)


def _gap(days):
    return {"poe1": {"since": (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "last_league": "Allflame"}}


def test_run_green_during_a_tolerated_gap_even_though_poe1_prices_meta_and_skills_are_stale(capsys):
    now = datetime.now(timezone.utc)
    with patch("requests.get", side_effect=_poe1_in_gap_sequence(now, _hb_status(now, gap=_gap(3)))):
        assert heartbeat_check.run() == 0
    out = capsys.readouterr().out
    assert "::notice::poe1: between leagues for" in out and "::error::" not in out


def test_run_red_when_the_gap_is_older_than_seven_days(capsys):
    now = datetime.now(timezone.utc)
    with patch("requests.get", side_effect=_poe1_in_gap_sequence(now, _hb_status(now, gap=_gap(8)))):
        assert heartbeat_check.run() == 1
    out = capsys.readouterr().out
    assert "poe1: no active league for" in out and "poe1: prices/latest.json" in out   # not excused any more


def test_run_without_the_key_is_the_old_strict_behaviour():
    now = datetime.now(timezone.utc)
    with patch("requests.get", side_effect=_poe1_in_gap_sequence(now, _hb_status(now, gap=None))):
        assert heartbeat_check.run() == 1


def test_run_a_gap_never_excuses_the_other_game(capsys):
    now = datetime.now(timezone.utc)
    seq = _poe1_in_gap_sequence(now, _hb_status(now, gap=_gap(3)), poe2_prices=_stale_prices(now))
    with patch("requests.get", side_effect=seq):
        assert heartbeat_check.run() == 1
    assert "poe2: prices/latest.json" in capsys.readouterr().out


def test_run_a_gap_never_excuses_news_of_that_game(capsys):
    now = datetime.now(timezone.utc)
    stale_news = _json_resp({"generated_at": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    seq = _poe1_in_gap_sequence(now, _hb_status(now, gap=_gap(3)), poe1_news=stale_news)
    with patch("requests.get", side_effect=seq):
        assert heartbeat_check.run() == 1
    assert "poe1: news/latest.json" in capsys.readouterr().out


def _fresh_poe1_sequence(now, status):
    return ([_fresh_prices_resp(now), _fresh_prices_resp(now), _fresh_news_resp(now), _fresh_news_resp(now),
             _index_resp(), _index_resp(), status, _fresh_skills_resp(now), _fresh_skills_resp(now)]
            + _weekly_fresh_responses(now))


@pytest.mark.parametrize("age,expected", [(timedelta(hours=1), 0), (timedelta(hours=47), 0),
                                          (timedelta(hours=50), 1), (None, 1)])
def test_run_launch_grace_for_an_empty_sample(age, expected, capsys):
    now = datetime.now(timezone.utc)
    first = (now - age).strftime("%Y-%m-%dT%H:%M:%SZ") if age is not None else None
    status = _hb_status(now, poe1_meta_age=timedelta(hours=2), poe1_sample=0, first_snapshot=first)
    with patch("requests.get", side_effect=_fresh_poe1_sequence(now, status)):
        assert heartbeat_check.run() == expected
    assert ("sample_total is 0 but the league's first snapshot" in capsys.readouterr().out) == (expected == 0)
