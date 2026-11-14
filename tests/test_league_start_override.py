"""`league.start_overrides.<game>.<league>` (config.yaml): a launch time that beats Steam's "Now Available" post.

Real case: Covens of Umbra (PoE1). Steam's "Path of Exile: Covens of Umbra is Now Available!" is dated 2026-11-06
23:00:24Z (the ExileCon reveal) while the league opened 2026-11-13, so every Covens snapshot got
days_since_league_start 6 instead of 0 and the archive's age buckets would have been shifted by 7 days.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.fetchers.league_dates import LeagueDates, start_override_from_cfg
from src.fetchers.league_detector import LeagueResult
from src.main import _detect_league_and_dates

COVENS_START = datetime(2026, 11, 13, 19, 0, 0, tzinfo=timezone.utc)
STEAM_START = datetime(2026, 11, 6, 23, 0, 24, tzinfo=timezone.utc)
STEAM_END = datetime(2027, 3, 1, 20, 0, 0, tzinfo=timezone.utc)


def _cfg(overrides):
    return {"league": {"cache_ttl_seconds": 1, "start_overrides": overrides}}


def test_override_is_parsed_as_utc():
    cfg = _cfg({"poe1": {"Covens": "2026-11-13T19:00:00Z"}})
    assert start_override_from_cfg(cfg, "poe1", "Covens") == COVENS_START


def test_override_with_a_non_utc_offset_is_converted():
    cfg = _cfg({"poe1": {"Covens": "2026-11-13T11:00:00-08:00"}})
    assert start_override_from_cfg(cfg, "poe1", "Covens") == COVENS_START


def test_unquoted_yaml_timestamp_is_accepted():
    cfg = yaml.safe_load("league:\n  start_overrides:\n    poe1:\n      Covens: 2026-11-13T19:00:00Z\n")
    assert start_override_from_cfg(cfg, "poe1", "Covens") == COVENS_START


@pytest.mark.parametrize("cfg,game,league", [
    ({}, "poe1", "Covens"),
    ({"league": {}}, "poe1", "Covens"),
    (_cfg({}), "poe1", "Covens"),
    (_cfg({"poe1": {"Covens": "2026-11-13T19:00:00Z"}}), "poe1", "Allflame"),     # another league: no effect
    (_cfg({"poe1": {"Covens": "2026-11-13T19:00:00Z"}}), "poe2", "Covens"),       # another game: no effect
    (_cfg({"poe1": None}), "poe1", "Covens"),
])
def test_no_override_means_none(cfg, game, league):
    assert start_override_from_cfg(cfg, game, league) is None


@pytest.mark.parametrize("bad", ["next friday", "2026-13-45T00:00:00Z", "2026-11-13T19:00:00", "", 12345])
def test_a_bad_override_is_ignored_loudly_not_silently(bad, caplog):
    with caplog.at_level(logging.ERROR):
        assert start_override_from_cfg(_cfg({"poe1": {"Covens": bad}}), "poe1", "Covens") is None
    assert any("not a valid UTC timestamp" in r.message and "using Steam" in r.message for r in caplog.records)


def _detect(cfg, game="poe1", league="Covens"):
    with patch("src.main.LeagueDetector") as MockDetector, patch("src.main.LeagueDatesFetcher") as MockDates:
        MockDetector.return_value.detect.return_value = LeagueResult(main=league, events=[])
        MockDates.return_value.fetch.return_value = LeagueDates(start_at=STEAM_START, end_at=STEAM_END)
        return _detect_league_and_dates(MagicMock(), cfg, game)


def test_the_job_uses_the_override_not_steam_and_keeps_steams_end_date():
    league, dates, days = _detect(_cfg({"poe1": {"Covens": "2026-11-13T19:00:00Z"}}))
    assert league == "Covens"
    assert dates.start_at == COVENS_START                       # the override, not Steam's 2026-11-06
    assert dates.end_at == STEAM_END                            # only the start is overridden
    assert days == (datetime.now(timezone.utc) - COVENS_START).days
    assert days != (datetime.now(timezone.utc) - STEAM_START).days   # and it really differs from the Steam answer


def test_the_job_uses_steam_when_there_is_no_override_for_this_league():
    _league, dates, days = _detect(_cfg({"poe1": {"Covens": "2026-11-13T19:00:00Z"}}), league="Next League")
    assert dates.start_at == STEAM_START
    assert days == (datetime.now(timezone.utc) - STEAM_START).days


def test_the_job_logs_which_start_it_used(caplog):
    with caplog.at_level(logging.INFO):
        _detect(_cfg({"poe1": {"Covens": "2026-11-13T19:00:00Z"}}))
    assert any("league start from config override 2026-11-13 19:00:00+00:00" in r.message and "Steam said 2026-11-06" in r.message
               for r in caplog.records)


def test_the_real_config_has_the_covens_override_and_it_parses():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert start_override_from_cfg(cfg, "poe1", "Covens") == COVENS_START
    # the override is exactly 7 days (and a bit) after the Steam post that fooled the hub
    assert timedelta(days=6) < COVENS_START - STEAM_START < timedelta(days=7)
