"""The league gap (2026-11-14): the stretch between two challenge leagues, when poe.ninja lists no non-permanent
league for a game. Covers the pure state helpers, the detector's dedicated error, the hub's three jobs (prices, slot
meta, skill meta), the Layer-1 ping rules (gap tolerated <= 7 days, launch grace 48 h, at least one real game).
(The Layer-2 heartbeat half lives in tests/test_league_gap_heartbeat.py: it ships separately, after the hub.)

Real shape being modelled: PoE1, Allflame ended 2026-11-09, Covens opened 2026-11-13, poe.ninja's list was
['Standard', 'Hardcore'] in between, and every PoE1 job raised "no eligible non-permanent league".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.fetchers.league_detector import LeagueDetector, NoActiveLeagueError
from src.main import (
    STATUS_BLOB_PATH,
    _all_games_got_real_characters,
    _all_games_got_real_skill_characters,
    run_meta_once,
    run_prices_once,
)
from src.processing import league_gap
from tests.test_main import _fake_meta_result, _fake_prices_publisher_factory, _meta_cfg, _skill_cfg

NOW = datetime(2026, 11, 14, 19, 0, 0, tzinfo=timezone.utc)


def _ts(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── pure helpers ───────────────────────────────────────────────────────────────────────────────

def test_note_gap_keeps_the_first_seen_time_and_only_moves_last_checked():
    status: dict = {"prices": {"poe1": {"league": "Allflame"}}}
    first = league_gap.note_gap(status, "poe1", NOW)
    later = league_gap.note_gap(status, "poe1", NOW + timedelta(hours=3))
    assert first["since"] == later["since"] == _ts(timedelta(0))
    assert later["last_checked_at"] == _ts(-timedelta(hours=3))
    assert later["last_league"] == "Allflame"           # from the last published prices
    assert set(status["league_gap"]) == {"poe1"}


def test_note_gap_replaces_an_unreadable_entry():
    status = {"league_gap": {"poe1": {"since": "yesterday-ish"}}}
    entry = league_gap.note_gap(status, "poe1", NOW)
    assert entry["since"] == _ts(timedelta(0))


def test_clear_gap_removes_the_entry_and_the_empty_mapping():
    status = {"league_gap": {"poe1": {"since": _ts(timedelta(days=2))}}}
    removed = league_gap.clear_gap(status, "poe1")
    assert removed == {"since": _ts(timedelta(days=2))}
    assert "league_gap" not in status                     # looks exactly like a status that never had the key
    assert league_gap.clear_gap(status, "poe1") is None   # nothing to clear -> None, no write needed


def test_clear_gap_keeps_the_other_games_entry():
    status = {"league_gap": {"poe1": {"since": _ts(timedelta(days=1))}, "poe2": {"since": _ts(timedelta(days=1))}}}
    league_gap.clear_gap(status, "poe1")
    assert set(status["league_gap"]) == {"poe2"}


@pytest.mark.parametrize("days,tolerated", [(0.0, True), (6.9, True), (7.0, True), (7.1, False), (30.0, False)])
def test_classify_tolerates_a_gap_up_to_max_days(days, tolerated):
    status = {"league_gap": {"poe1": {"since": (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")}}}
    ok, exceeded = league_gap.classify(status, {"poe1"}, 7, NOW)
    assert (("poe1" in ok), ("poe1" in exceeded)) == (tolerated, not tolerated)


def test_classify_with_no_record_is_tolerated_not_exceeded():
    ok, exceeded = league_gap.classify({}, {"poe1"}, 7, NOW)
    assert ok == {"poe1"} and exceeded == {}


def test_in_launch_grace_boundaries_and_fail_closed():
    assert league_gap.in_launch_grace(_ts(timedelta(hours=1)), NOW, 48)
    assert league_gap.in_launch_grace(_ts(timedelta(hours=48)), NOW, 48)
    assert not league_gap.in_launch_grace(_ts(timedelta(hours=48, seconds=1)), NOW, 48)
    assert not league_gap.in_launch_grace(None, NOW, 48)          # unknown first snapshot: the old strict rule
    assert not league_gap.in_launch_grace("garbage", NOW, 48)


def test_settings_defaults_and_config_values():
    assert league_gap.GapSettings.from_cfg({}) == league_gap.GapSettings(7, 48)
    assert league_gap.GapSettings.from_cfg({"league_gap": {"max_days": 3, "launch_grace_hours": 12}}) == league_gap.GapSettings(3, 12)


# ── the detector's dedicated error ─────────────────────────────────────────────────────────────

def test_detector_raises_no_active_league_when_only_permanent_leagues_are_listed(tmp_path):
    http = MagicMock()
    http.get_json.return_value = [{"id": "Standard", "name": "Standard"}, {"id": "Hardcore", "name": "Hardcore"}]
    detector = LeagueDetector(http, game="poe1", failed_responses_dir=tmp_path)
    with pytest.raises(NoActiveLeagueError) as exc:
        detector.detect()
    assert isinstance(exc.value, ValueError)              # every old `except ValueError` still catches it
    assert "no eligible non-permanent league" in str(exc.value)


def test_detector_still_raises_a_plain_error_for_a_garbage_response(tmp_path):
    http = MagicMock()
    http.get_json.return_value = []                        # an empty list is a broken response, not a gap
    detector = LeagueDetector(http, game="poe1", failed_responses_dir=tmp_path)
    with pytest.raises(ValueError) as exc:
        detector.detect()
    assert not isinstance(exc.value, NoActiveLeagueError)


# ── Layer-1 predicates ─────────────────────────────────────────────────────────────────────────

CFG2 = {"game_versions": ["poe2", "poe1"]}
SETTINGS = league_gap.GapSettings()


def _slot(sample_total, first_snapshot_at=None):
    result = _fake_meta_result("poe1", sample_total=sample_total)
    result["summary"]["league_first_snapshot_at"] = first_snapshot_at
    return result


def test_ping_predicate_original_rules_are_unchanged_without_gaps():
    ok = {"poe2": _fake_meta_result("poe2", 50), "poe1": _fake_meta_result("poe1", 30)}
    assert _all_games_got_real_characters(CFG2, ok)
    assert not _all_games_got_real_characters(CFG2, {"poe2": ok["poe2"], "poe1": _slot(0)})
    assert not _all_games_got_real_characters(CFG2, {"poe2": ok["poe2"]})


def test_ping_predicate_excuses_a_tolerated_gap_game_but_not_a_missing_one():
    only_poe2 = {"poe2": _fake_meta_result("poe2", 50)}
    assert _all_games_got_real_characters(CFG2, only_poe2, frozenset({"poe1"}), SETTINGS, NOW)
    assert not _all_games_got_real_characters(CFG2, only_poe2, frozenset(), SETTINGS, NOW)   # crashed, not a gap


def test_ping_predicate_needs_at_least_one_real_game():
    assert not _all_games_got_real_characters(CFG2, {}, frozenset({"poe1", "poe2"}), SETTINGS, NOW)
    both_new = {"poe2": _slot(0, _ts(timedelta(hours=1))), "poe1": _slot(0, _ts(timedelta(hours=1)))}
    assert not _all_games_got_real_characters(CFG2, both_new, frozenset(), SETTINGS, NOW)


@pytest.mark.parametrize("age,expected", [(timedelta(minutes=5), True), (timedelta(hours=47), True),
                                          (timedelta(hours=49), False), (None, False)])
def test_ping_predicate_launch_grace_for_an_empty_sample(age, expected):
    poe1 = _slot(0, _ts(age) if age is not None else None)
    results = {"poe2": _fake_meta_result("poe2", 50), "poe1": poe1}
    assert _all_games_got_real_characters(CFG2, results, frozenset(), SETTINGS, NOW) is expected


def test_skill_ping_predicate_has_the_same_two_exceptions():
    real = {"characters_distinct": 100}
    assert _all_games_got_real_skill_characters(CFG2, {"poe2": real}, frozenset({"poe1"}), SETTINGS, NOW)
    assert not _all_games_got_real_skill_characters(CFG2, {"poe2": real}, frozenset(), SETTINGS, NOW)
    fresh = {"characters_distinct": 0, "league_first_snapshot_at": _ts(timedelta(hours=2))}
    assert _all_games_got_real_skill_characters(CFG2, {"poe2": real, "poe1": fresh}, frozenset(), SETTINGS, NOW)
    old = {"characters_distinct": 0, "league_first_snapshot_at": _ts(timedelta(days=4))}
    assert not _all_games_got_real_skill_characters(CFG2, {"poe2": real, "poe1": old}, frozenset(), SETTINGS, NOW)


# ── the hub's jobs ─────────────────────────────────────────────────────────────────────────────

def _seed_status(factory, status: dict) -> None:
    factory(bucket_name="test-bucket").publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def _read_status(created) -> dict:
    return created[-1].fetch_previous(STATUS_BLOB_PATH) or {}


def _run_slot(tmp_path, cfg, *, gap_games=(), crash_games=(), results=None, seed=None, **kwargs):
    kwargs.setdefault("parts", ("slot",))   # the slot job alone, so the real skill job is never reached
    created: list = []
    factory = _fake_prices_publisher_factory(tmp_path, created)
    if seed is not None:
        _seed_status(factory, seed)

    def fake_slot(http, publisher, cfg, game):
        if game in gap_games:
            raise NoActiveLeagueError("no eligible non-permanent league found in ['Standard', 'Hardcore']")
        if game in crash_games:
            raise RuntimeError("boom")
        return (results or {}).get(game) or _fake_meta_result(game)

    with patch("src.main.PricesPublisher", new=factory), \
         patch("src.main._run_meta_for_game", side_effect=fake_slot), \
         patch("src.main.HttpClient") as MockHttp:
        run_meta_once(cfg, **kwargs)
    return MockHttp.return_value, _read_status(created)


def test_slot_job_keeps_pinging_when_one_game_is_in_a_gap(tmp_path):
    http, status = _run_slot(tmp_path, _meta_cfg(), gap_games={"poe1"})
    http.get_bytes.assert_called_once_with("https://hc-ping.com/test-meta")
    assert status["league_gap"]["poe1"]["since"]                # recorded, with its first-seen time
    assert "poe1" not in status["meta"]                          # nothing was published for it, last-known entry stays


def test_slot_job_stops_pinging_once_the_gap_is_older_than_seven_days(tmp_path):
    since = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = {"league_gap": {"poe1": {"since": since, "last_league": "Allflame"}}}
    http, status = _run_slot(tmp_path, _meta_cfg(), gap_games={"poe1"}, seed=old)
    http.get_bytes.assert_not_called()
    assert status["league_gap"]["poe1"]["since"] == since        # the first-seen time is not reset


def test_slot_job_does_not_ping_when_a_game_crashes_for_another_reason(tmp_path):
    http, status = _run_slot(tmp_path, _meta_cfg(), crash_games={"poe1"})
    http.get_bytes.assert_not_called()
    assert "league_gap" not in status                            # only NoActiveLeagueError counts as a gap


def test_slot_job_does_not_ping_when_every_game_is_in_a_gap(tmp_path):
    http, _status = _run_slot(tmp_path, _meta_cfg(), gap_games={"poe1", "poe2"})
    http.get_bytes.assert_not_called()


def test_slot_job_pings_for_an_empty_sample_inside_the_launch_grace(tmp_path):
    fresh = _slot(0, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    http, _ = _run_slot(tmp_path, _meta_cfg(), results={"poe1": fresh})
    http.get_bytes.assert_called_once_with("https://hc-ping.com/test-meta")


def test_slot_job_does_not_ping_for_an_empty_sample_after_the_grace(tmp_path):
    stale = _slot(0, (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    http, _ = _run_slot(tmp_path, _meta_cfg(), results={"poe1": stale})
    http.get_bytes.assert_not_called()


def test_slot_job_clears_the_gap_itself_only_when_the_skill_part_is_not_part_of_the_run(tmp_path):
    seed = {"league_gap": {"poe1": {"since": _ts(timedelta(days=2)), "last_league": "Allflame"}}}
    # slot + skill in the run: the SKILL job is the one that clears it (the last of the daily jobs)
    created: list = []
    factory = _fake_prices_publisher_factory(tmp_path, created)
    _seed_status(factory, seed)
    with patch("src.main.PricesPublisher", new=factory), \
         patch("src.main._run_meta_for_game", side_effect=lambda h, p, c, g: _fake_meta_result(g)), \
         patch("src.main._run_skill_meta_for_game", side_effect=RuntimeError("skill job broke")), \
         patch("src.main.HttpClient"):
        run_meta_once(_meta_cfg())
    assert "poe1" in _read_status(created).get("league_gap", {})        # slot success alone did not clear it

    # slot only: nothing later in the day will clear it, so the slot job does
    _, status = _run_slot(tmp_path, _meta_cfg(), seed=seed, parts=("slot",))
    assert "league_gap" not in status


def _run_skill(tmp_path, cfg, *, gap_games=(), results=None, seed=None, **kwargs):
    created: list = []
    factory = _fake_prices_publisher_factory(tmp_path, created)
    if seed is not None:
        _seed_status(factory, seed)

    def fake_skill(http, publisher, cfg, game):
        if game in gap_games:
            raise NoActiveLeagueError("no eligible non-permanent league found in ['Standard', 'Hardcore']")
        return (results or {}).get(game) or {"characters_distinct": 10}

    with patch("src.main.PricesPublisher", new=factory), \
         patch("src.main._run_skill_meta_for_game", side_effect=fake_skill), \
         patch("src.main.HttpClient") as MockHttp:
        run_meta_once(cfg, games=["poe1", "poe2"], parts=("skill",), **kwargs)
    return MockHttp.return_value, _read_status(created)


def test_skill_job_keeps_pinging_when_one_game_is_in_a_gap(tmp_path):
    """The 2026-11-11..13 case: skill_meta 'down 1 day 19 hours' because PoE1 had no league."""
    http, status = _run_skill(tmp_path, _skill_cfg(), gap_games={"poe1"})
    http.get_bytes.assert_called_once_with("https://hc-ping.com/test-skill-meta")
    assert "poe1" in status["league_gap"]


def test_skill_job_stops_pinging_after_seven_days(tmp_path):
    seed = {"league_gap": {"poe1": {"since": (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")}}}
    http, _ = _run_skill(tmp_path, _skill_cfg(), gap_games={"poe1"}, seed=seed)
    http.get_bytes.assert_not_called()


def test_skill_job_clears_the_gap_when_the_league_is_back_and_pings(tmp_path):
    seed = {"league_gap": {"poe1": {"since": _ts(timedelta(days=4)), "last_league": "Allflame"}}}
    http, status = _run_skill(tmp_path, _skill_cfg(), seed=seed)
    http.get_bytes.assert_called_once_with("https://hc-ping.com/test-skill-meta")
    assert "league_gap" not in status


def test_skill_job_pings_for_an_empty_sample_inside_the_launch_grace(tmp_path):
    fresh = {"characters_distinct": 0, "league_first_snapshot_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    http, _ = _run_skill(tmp_path, _skill_cfg(), results={"poe1": fresh})
    http.get_bytes.assert_called_once_with("https://hc-ping.com/test-skill-meta")


def test_prices_job_treats_a_gap_as_expected_and_still_pings(tmp_path):
    cfg = {"firebase": {"bucket": "test-bucket"}, "game_versions": ["poe2", "poe1"], "rate_limits": {}, "http": {},
           "healthchecks": {"prices_ping_url": "https://hc-ping.com/test-prices"}}
    created: list = []

    def fake_prices(http, publisher, cfg, game):
        if game == "poe1":
            raise NoActiveLeagueError("no eligible non-permanent league found in ['Standard', 'Hardcore']")
        return {"ok": True, "generated_at": "2026-11-14T16:53:00Z", "league": "Forbidden Rites", "events": [],
                "hardcore": None, "sources": {"ninja": "ok"}}

    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created)), \
         patch("src.main._run_prices_for_game", side_effect=fake_prices), \
         patch("src.main.HttpClient") as MockHttp:
        run_prices_once(cfg)
    status = _read_status(created)
    assert "poe1" in status["league_gap"] and "poe1" not in status["prices"]
    assert status["prices"]["poe2"]["league"] == "Forbidden Rites"
    assert not status["errors"]                                  # a gap is not an error entry
    MockHttp.return_value.get_bytes.assert_called_once_with("https://hc-ping.com/test-prices")


def test_prices_job_still_records_a_real_failure_as_an_error(tmp_path):
    cfg = {"firebase": {"bucket": "test-bucket"}, "game_versions": ["poe1"], "rate_limits": {}, "http": {}, "healthchecks": {}}
    created: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created)), \
         patch("src.main._run_prices_for_game", side_effect=RuntimeError("boom")), \
         patch("src.main.HttpClient"):
        run_prices_once(cfg)
    status = _read_status(created)
    assert "league_gap" not in status and status["errors"]
