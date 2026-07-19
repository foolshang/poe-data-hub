"""Unit tests for src/main.py's status.json staleness monitoring
(_check_stats_cache_stale), the meta status update flow, the Layer-1
healthchecks.io dead-man-switch ping helper, and (phase 7) the multi-game /
multi-league prices job (run_prices_once, _update_status)."""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.fetchers.league_detector import LeagueResult
from src.main import (
    UNMAPPED_HISTORY_LEN,
    _check_league_start_mode,
    _check_stats_cache_stale,
    _compute_meta_trigger,
    _ping_healthcheck,
    _update_meta_status,
    _update_status,
    run_meta_once,
    run_prices_once,
    run_scheduler,
)
from src.storage.publisher import PricesPublisher
from src.utils import job_lock


def test_ping_healthcheck_noop_when_url_missing():
    http = MagicMock()
    _ping_healthcheck(http, None, "prices")
    _ping_healthcheck(http, "", "prices")
    http.get_bytes.assert_not_called()


def test_ping_healthcheck_calls_get_bytes_when_url_set():
    http = MagicMock()
    _ping_healthcheck(http, "https://hc-ping.com/some-uuid", "prices")
    http.get_bytes.assert_called_once_with("https://hc-ping.com/some-uuid")


def test_ping_healthcheck_failure_does_not_raise():
    http = MagicMock()
    http.get_bytes.side_effect = RuntimeError("network blip")
    _ping_healthcheck(http, "https://hc-ping.com/some-uuid", "meta")  # must not raise


def test_no_reasons_when_fresh_and_in_line_with_history():
    reasons = _check_stats_cache_stale(
        unmapped_history=[2.0, 2.5, 2.1, 2.3], stats_fetched_at=time.time(), unmapped_pct=2.4
    )
    assert reasons == []


def test_flags_cache_older_than_30_days():
    old_ts = time.time() - 31 * 86400
    reasons = _check_stats_cache_stale(unmapped_history=[], stats_fetched_at=old_ts, unmapped_pct=1.0)
    assert len(reasons) == 1
    assert "days old" in reasons[0]


def test_does_not_flag_cache_exactly_at_boundary():
    ts = time.time() - 29 * 86400
    reasons = _check_stats_cache_stale(unmapped_history=[], stats_fetched_at=ts, unmapped_pct=1.0)
    assert reasons == []


def test_flags_unmapped_spike_over_2x_average():
    reasons = _check_stats_cache_stale(
        unmapped_history=[2.0, 2.0, 2.0, 2.0], stats_fetched_at=time.time(), unmapped_pct=5.0
    )
    assert len(reasons) == 1
    assert "2x recent average" in reasons[0]


def test_does_not_flag_unmapped_spike_at_exactly_2x():
    reasons = _check_stats_cache_stale(
        unmapped_history=[2.0, 2.0, 2.0, 2.0], stats_fetched_at=time.time(), unmapped_pct=4.0
    )
    assert reasons == []


def test_both_reasons_can_fire_together():
    old_ts = time.time() - 40 * 86400
    reasons = _check_stats_cache_stale(unmapped_history=[1.0, 1.0], stats_fetched_at=old_ts, unmapped_pct=10.0)
    assert len(reasons) == 2


def test_no_stats_fetched_at_only_checks_history():
    reasons = _check_stats_cache_stale(unmapped_history=[1.0, 1.0], stats_fetched_at=None, unmapped_pct=1.0)
    assert reasons == []


def _meta_result(pct, fetched_at=None, league="Runes of Aldur"):
    return {
        "data": {"generated_at": "2026-07-10T00:00:00Z", "league": league},
        "end_at": None,
        "summary": {"unmapped_mod_pct": pct, "stats_cache_fetched_at": fetched_at or time.time()},
    }


def test_update_meta_status_tracks_rolling_history(tmp_path):
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()

    for pct in [2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7]:
        _update_meta_status(publisher, {"poe2": _meta_result(pct)})

    status = publisher.fetch_previous("status.json")
    history = status["meta_unmapped_history"]["poe2"]
    assert len(history) == UNMAPPED_HISTORY_LEN
    # oldest entry (2.0) should have been dropped once the window filled past 7
    assert 2.0 not in history
    assert status["meta"]["poe2"]["stats_cache_stale"] is False


def test_update_meta_status_flags_stale_and_logs_error(tmp_path):
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()

    old_ts = time.time() - 45 * 86400
    _update_meta_status(publisher, {"poe2": _meta_result(1.0, fetched_at=old_ts)})

    status = publisher.fetch_previous("status.json")
    assert status["meta"]["poe2"]["stats_cache_stale"] is True
    assert any("stats_cache_stale" in e["detail"] for e in status["errors"])


def test_update_meta_status_tracks_poe1_and_poe2_histories_independently(tmp_path):
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()

    _update_meta_status(publisher, {
        "poe2": _meta_result(2.0, league="Runes of Aldur"),
        "poe1": _meta_result(9.0, league="Mirage"),
    })

    status = publisher.fetch_previous("status.json")
    assert status["meta"]["poe2"]["league"] == "Runes of Aldur"
    assert status["meta"]["poe1"]["league"] == "Mirage"
    assert status["meta_unmapped_history"]["poe2"] == [2.0]
    assert status["meta_unmapped_history"]["poe1"] == [9.0]
    # poe1's high unmapped_mod_pct crosses the 5% threshold -> its own error, unrelated to poe2
    assert any("meta(poe1)" == e["job"] for e in status["errors"])
    assert not any("meta(poe2)" == e["job"] for e in status["errors"])


def test_update_meta_status_keeps_other_game_untouched_when_one_fails(tmp_path):
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()

    _update_meta_status(publisher, {
        "poe2": _meta_result(2.0, league="Runes of Aldur"),
        "poe1": _meta_result(3.0, league="Mirage"),
    })
    # next run: poe1 failed entirely (absent from results) -- poe2 updates,
    # poe1's last-known entry must survive untouched.
    _update_meta_status(publisher, {"poe2": _meta_result(2.5, league="Runes of Aldur")})

    status = publisher.fetch_previous("status.json")
    assert status["meta"]["poe1"]["league"] == "Mirage"  # untouched
    assert status["meta_unmapped_history"]["poe2"] == [2.0, 2.5]


def _fake_meta_result(game: str) -> dict:
    league = "Runes of Aldur" if game == "poe2" else "Mirage"
    return {
        "data": {
            "schema_version": 2, "generated_at": "2026-07-15T02:00:00Z", "league": league,
            "mods": [], "sample": {"per_slot": {}, "unique_characters_fetched": 10, "fetch_failed": 0},
            "archetype_distribution": [], "stat_dictionary": [], "days_since_league_start": 5,
        },
        "end_at": None,
        "summary": {"unmapped_mod_pct": 1.0, "stats_cache_fetched_at": 1752500000.0, "duration_seconds": 1.0},
    }


def test_run_meta_once_publishes_both_games_and_pings_once(tmp_path):
    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2", "poe1"],
        "rate_limits": {},
        "http": {},
        "healthchecks": {"meta_ping_url": "https://hc-ping.com/test-meta"},
    }

    created_publishers: list = []

    def fake_run_meta_for_game(http, publisher, cfg, game):
        return _fake_meta_result(game)

    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main._run_meta_for_game", side_effect=fake_run_meta_for_game), \
         patch("src.main.HttpClient") as MockHttpClient:
        mock_http = MockHttpClient.return_value
        result = run_meta_once(cfg)

    # one ping for the whole job, not per game
    mock_http.get_bytes.assert_called_once_with("https://hc-ping.com/test-meta")

    # run_meta_once's own return value + status.json are what it actually
    # builds from _run_meta_for_game's results (the per-game latest.json/
    # dated-file publish happens *inside* the mocked-out function, so it's
    # covered by _run_meta_for_game's own tests / the live smoke test, not
    # re-asserted here -- this test is about the orchestration layer).
    assert result["poe2"]["league"] == "Runes of Aldur"
    assert result["poe1"]["league"] == "Mirage"

    publisher = created_publishers[-1]
    status = publisher.fetch_previous("status.json")
    assert status["meta"]["poe2"]["league"] == "Runes of Aldur"
    assert status["meta"]["poe1"]["league"] == "Mirage"


def test_run_meta_once_one_game_failure_does_not_block_the_other(tmp_path):
    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2", "poe1"],
        "rate_limits": {},
        "http": {},
        "healthchecks": {},
    }

    def fake_run_meta_for_game(http, publisher, cfg, game):
        if game == "poe2":
            raise RuntimeError("simulated poe2 meta failure")
        return _fake_meta_result(game)

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main._run_meta_for_game", side_effect=fake_run_meta_for_game):
        result = run_meta_once(cfg)

    assert "poe2" not in result
    assert result["poe1"]["league"] == "Mirage"

    publisher = created_publishers[-1]
    status = publisher.fetch_previous("status.json")
    assert "poe2" not in status.get("meta", {})
    assert status["meta"]["poe1"]["league"] == "Mirage"


def test_update_status_keeps_other_games_untouched_on_partial_failure(tmp_path):
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()

    _update_status(
        publisher, ok=True,
        per_game={
            "poe2": {"generated_at": "2026-07-11T00:00:00Z", "league": "Runes of Aldur", "events": [], "sources": {"ninja": "ok", "scout": "ok"}},
            "poe1": {"generated_at": "2026-07-11T00:00:00Z", "league": "Mirage", "events": ["Ancestors"], "sources": {"ninja": "ok"}},
        },
    )
    status = publisher.fetch_previous("status.json")
    assert status["prices"]["poe2"]["league"] == "Runes of Aldur"
    assert status["prices"]["poe1"]["events"] == ["Ancestors"]
    assert "scout" not in status["prices"]["poe1"]["sources"]

    # Next run: poe1 failed entirely (not present in per_game) -- poe2 updates,
    # poe1's last-known entry must survive untouched (stale-while-error).
    _update_status(
        publisher, ok=False,
        per_game={
            "poe2": {"generated_at": "2026-07-11T00:20:00Z", "league": "Runes of Aldur", "events": [], "sources": {"ninja": "ok", "scout": "ok"}},
        },
    )
    status = publisher.fetch_previous("status.json")
    assert status["prices"]["poe2"]["generated_at"] == "2026-07-11T00:20:00Z"
    assert status["prices"]["poe1"]["generated_at"] == "2026-07-11T00:00:00Z"  # untouched
    assert status["errors"][-1]["job"] == "prices"


def test_run_meta_once_skips_when_lock_already_held(tmp_path):
    """2026-07-15 fix: a second run_meta_once (from either the scheduler or
    a manual CLI retry) must never proceed while another is already
    running -- it must skip cleanly instead of colliding with it over the
    same rate-limited hosts (see CLAUDE.md's incident writeup)."""
    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2", "poe1"],
        "rate_limits": {},
        "http": {},
        "healthchecks": {},
    }
    created_publishers: list = []

    with patch("src.main.job_lock.DEFAULT_LOCK_DIR", tmp_path / "locks"), \
         patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main._run_meta_for_game") as fake_run_meta_for_game:
        with job_lock.acquire("meta", lock_dir=tmp_path / "locks"):
            result = run_meta_once(cfg)

    assert result == {}
    fake_run_meta_for_game.assert_not_called()
    assert created_publishers == []  # never even got as far as building a publisher


def test_run_meta_once_acquires_and_releases_lock_around_a_normal_run(tmp_path):
    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2"],
        "rate_limits": {},
        "http": {},
        "healthchecks": {},
    }
    created_publishers: list = []

    def fake_run_meta_for_game(http, publisher, cfg, game):
        return _fake_meta_result(game)

    lock_dir = tmp_path / "locks"
    with patch("src.main.job_lock.DEFAULT_LOCK_DIR", lock_dir), \
         patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main._run_meta_for_game", side_effect=fake_run_meta_for_game), \
         patch("src.main.HttpClient"):
        assert job_lock.read_lock("meta", lock_dir=lock_dir) is None
        run_meta_once(cfg)
        # lock must be released again once the run completes
        assert job_lock.read_lock("meta", lock_dir=lock_dir) is None


def _fake_prices_publisher_factory(tmp_path, created: list):
    """One shared _FakeStorageClient across every PricesPublisher the
    factory produces -- matches production, where a fresh PricesPublisher
    object still talks to the same real GCS bucket regardless of how many
    times main.py constructs one (e.g. once per run_news_once call)."""
    shared_client = _FakeStorageClient()

    def factory(*, bucket_name, project_id=None):
        p = PricesPublisher(bucket_name=bucket_name, local_cache_dir=tmp_path)
        p._client = shared_client
        created.append(p)
        return p
    return factory


def test_run_prices_once_publishes_events_and_index_per_game(tmp_path):
    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2", "poe1"],
        "league": {"cache_ttl_seconds": 43200},
        "rate_limits": {},
        "http": {},
        "healthchecks": {},
    }

    fake_ninja_instance = MagicMock()
    fake_ninja_instance.fetch_all.return_value = (
        [{"category": "Currency", "name": "Chaos Orb", "value": 1, "value_currency": "exalted", "divine_value": 0.01, "source": "ninja"}],
        {"Currency": {"ok": True, "count": 1}},
    )
    fake_scout_instance = MagicMock()
    fake_scout_instance.fetch_all.return_value = ([], {})

    def detector_factory(http, game, league_override, cache_ttl_seconds):
        d = MagicMock()
        d.detect.return_value = (
            LeagueResult(main="Runes of Aldur", events=["Some Event"], hardcore="HC Runes of Aldur")
            if game == "poe2"
            else LeagueResult(main="Mirage", events=[], hardcore=None)
        )
        return d

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main.NinjaPricesFetcher", return_value=fake_ninja_instance) as NinjaCls, \
         patch("src.main.ScoutPricesFetcher", return_value=fake_scout_instance) as ScoutCls, \
         patch("src.main.LeagueDetector", side_effect=detector_factory):
        run_prices_once(cfg)

    # ScoutPricesFetcher (poe2scout.com, PoE2-only) must be constructed only
    # for poe2's main + event + hardcore leagues, never for poe1.
    assert ScoutCls.call_count == 3

    assert len(created_publishers) == 1
    publisher = created_publishers[0]
    poe2_index = publisher.fetch_previous("poe2/prices/index.json")
    assert poe2_index["main"]["league"] == "Runes of Aldur"
    assert len(poe2_index["events"]) == 1
    assert poe2_index["events"][0]["path"] == "poe2/prices/events/some-event.json"

    poe2_event = publisher.fetch_previous("poe2/prices/events/some-event.json")
    assert poe2_event["league"] == "Some Event"
    assert poe2_event["sources"] == {"ninja": "ok", "scout": "ok"}
    assert poe2_event["sample"]["per_category"]["ninja"] == {"Currency": {"ok": True, "count": 1}}

    poe1_latest = publisher.fetch_previous("poe1/prices/latest.json")
    assert poe1_latest["league"] == "Mirage"
    assert "scout" not in poe1_latest["sources"]

    poe1_index = publisher.fetch_previous("poe1/prices/index.json")
    assert poe1_index["events"] == []
    assert poe1_index["hardcore"] is None  # no HC variant found for poe1 this run

    poe2_hardcore = publisher.fetch_previous("poe2/prices/hardcore/latest.json")
    assert poe2_hardcore["league"] == "HC Runes of Aldur"
    assert poe2_index["hardcore"]["league"] == "HC Runes of Aldur"
    assert poe2_index["hardcore"]["path"] == "poe2/prices/hardcore/latest.json"

    status = publisher.fetch_previous("status.json")
    assert status["prices"]["poe2"]["league"] == "Runes of Aldur"
    assert status["prices"]["poe2"]["hardcore"] == "HC Runes of Aldur"
    assert status["prices"]["poe1"]["hardcore"] is None
    assert status["prices"]["poe1"]["league"] == "Mirage"


def test_run_prices_once_removes_stale_hardcore_file_when_variant_disappears(tmp_path):
    """index.hardcore == null must mean the file genuinely doesn't exist,
    not just "we didn't update it this run" -- simulates a league-transition
    window where the HC variant briefly can't be found."""
    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2"],
        "league": {"cache_ttl_seconds": 43200},
        "rate_limits": {},
        "http": {},
        "healthchecks": {},
    }

    fake_ninja_instance = MagicMock()
    fake_ninja_instance.fetch_all.return_value = (
        [{"category": "Currency", "name": "Chaos Orb", "value": 1, "value_currency": "exalted", "divine_value": 0.01, "source": "ninja"}],
        {"Currency": {"ok": True, "count": 1}},
    )
    fake_scout_instance = MagicMock()
    fake_scout_instance.fetch_all.return_value = ([], {})

    hardcore_present = [True]  # mutable flag flipped between the two runs

    def detector_factory(http, game, league_override, cache_ttl_seconds):
        d = MagicMock()
        d.detect.return_value = LeagueResult(
            main="Runes of Aldur", events=[],
            hardcore="HC Runes of Aldur" if hardcore_present[0] else None,
        )
        return d

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main.NinjaPricesFetcher", return_value=fake_ninja_instance), \
         patch("src.main.ScoutPricesFetcher", return_value=fake_scout_instance), \
         patch("src.main.LeagueDetector", side_effect=detector_factory):
        run_prices_once(cfg)  # run 1: hardcore present -> file published
        hardcore_present[0] = False
        run_prices_once(cfg)  # run 2: hardcore gone -> file must be removed

    publisher = created_publishers[-1]
    assert "poe2/prices/hardcore/latest.json" not in publisher._client._blobs
    poe2_index = publisher.fetch_previous("poe2/prices/index.json")
    assert poe2_index["hardcore"] is None


def test_run_news_once_publishes_per_game_news_and_shared_mtx(tmp_path):
    from src.main import run_news_once

    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2", "poe1"],
        "rate_limits": {},
        "http": {},
        "healthchecks": {},
    }

    def fake_fetch_forum_news(http, game, previous_entries):
        return [{"id": f"forum-{game}-1", "title": f"{game} news", "category": "news", "link": "https://www.pathofexile.com/x", "content": "", "published": "2026-07-12T00:00:00Z"}]

    def fake_fetch_deals(http):
        return [{"item_name": "Premium Quad Stash Tab", "link": "x", "discount_percent": 20, "points_price": 120, "ends_in": "Ends in 1 day"}]

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main.news.fetch_forum_news", side_effect=fake_fetch_forum_news), \
         patch("src.main.deals.fetch_deals", side_effect=fake_fetch_deals):
        result1 = run_news_once(cfg)
        result2 = run_news_once(cfg)  # second run: mtx first_seen_at must carry over

    assert result1["ok"] is True
    assert result2["ok"] is True

    publisher = created_publishers[-1]
    poe2_news = publisher.fetch_previous("poe2/news/latest.json")
    assert poe2_news["entries"][0]["id"] == "forum-poe2-1"
    assert poe2_news["sources"] == {"forum": "ok"}
    assert not poe2_news["entries"][0]["link"].startswith("https://th.pathofexile.com")

    poe1_news = publisher.fetch_previous("poe1/news/latest.json")
    assert poe1_news["entries"][0]["id"] == "forum-poe1-1"

    # first_seen_at's exact carry-over-vs-reset behavior is unit-tested precisely
    # in test_publisher.py (distinct previous/now timestamps there); this just
    # confirms main.py actually wires build_mtx_schema's previous= through and
    # publishes to the right path.
    mtx = publisher.fetch_previous("mtx/latest.json")
    assert len(mtx["deals"]) == 1
    assert mtx["deals"][0]["item_name"] == "Premium Quad Stash Tab"
    assert "first_seen_at" in mtx["deals"][0]


def test_run_news_once_fetch_failure_reuses_previous_news(tmp_path):
    from src.main import run_news_once

    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "game_versions": ["poe2"],
        "rate_limits": {},
        "http": {},
        "healthchecks": {},
    }

    def fake_fetch_forum_news_ok(http, game, previous_entries):
        return [{"id": "forum-1", "title": "first run", "category": "news", "link": "x", "content": "", "published": "2026-07-12T00:00:00Z"}]

    def fake_fetch_forum_news_fail(http, game, previous_entries):
        raise RuntimeError("forum down")

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main.deals.fetch_deals", return_value=[]):
        with patch("src.main.news.fetch_forum_news", side_effect=fake_fetch_forum_news_ok):
            run_news_once(cfg)
        with patch("src.main.news.fetch_forum_news", side_effect=fake_fetch_forum_news_fail):
            result = run_news_once(cfg)

    assert result["ok"] is False
    # each run_news_once call constructs its own PricesPublisher -- read back
    # from the second call's instance, not the first, to see what it actually
    # published (they share the same on-disk cache dir/Firebase bucket in
    # production; the fake in-memory client here is per-instance only).
    publisher = created_publishers[-1]
    poe2_news = publisher.fetch_previous("poe2/news/latest.json")
    assert poe2_news["sources"] == {"forum": "error"}
    assert poe2_news["entries"][0]["id"] == "forum-1"  # stale-while-error reused the first run's entry


def test_run_scheduler_uses_daily_cron_when_meta_runs_per_day_is_one(tmp_path):
    from apscheduler.triggers.cron import CronTrigger

    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "schedule": {"meta_cron_hour_thailand": 2, "meta_runs_per_day_league_start": 1},
    }
    fake_scheduler = MagicMock()
    with patch("src.main.BlockingScheduler", return_value=fake_scheduler), \
         patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, [])):
        run_scheduler(cfg)

    meta_call = next(c for c in fake_scheduler.add_job.call_args_list if c.kwargs.get("id") == "meta")
    trigger = meta_call.args[1]
    assert isinstance(trigger, CronTrigger)


def test_run_scheduler_switches_to_evenly_spaced_runs_for_league_start(tmp_path):
    """League-start mode (spec 9): bumping meta_runs_per_day_league_start
    above 1 must switch the meta job from a once-daily cron to N
    evenly-spaced runs/day, via this one config line -- no code change.
    Confirmed here since the pipeline's own SPEC.md relies on this switch
    being ready before the next PoE1 league launch (2026-07-25). This is
    the manual escape-hatch path (no league_start_mode configured) --
    see test_compute_meta_trigger_* for the automatic date-based switch."""
    from apscheduler.triggers.interval import IntervalTrigger

    cfg = {
        "firebase": {"bucket": "test-bucket"},
        "schedule": {"meta_cron_hour_thailand": 2, "meta_runs_per_day_league_start": 4},
    }
    fake_scheduler = MagicMock()
    with patch("src.main.BlockingScheduler", return_value=fake_scheduler), \
         patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, [])):
        run_scheduler(cfg)

    meta_call = next(c for c in fake_scheduler.add_job.call_args_list if c.kwargs.get("id") == "meta")
    trigger = meta_call.args[1]
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.total_seconds() == 6 * 3600  # 24h / 4 runs


# ── league_start_mode: per-game manual/auto date switch (spec 9, 2026-07-19/20) ──

_LEAGUE_START_CFG = {
    "meta_cron_hour_thailand": 2,
    "meta_runs_per_day_league_start": 1,
    "league_start_mode": {
        "meta_runs_per_day": 4,
        "poe1": {"start_date": "2026-07-25", "end_date": "2026-08-15"},
    },
}
_GAME_VERSIONS_CFG = {"game_versions": ["poe1", "poe2"]}
SAMPLES_ROOT = Path(__file__).resolve().parents[1] / "data" / "samples"


class _FakeSteamHttp:
    """Minimal HttpClient stand-in for league_start_mode tests --
    returns empty Steam news (no upcoming-launch match, i.e. auto mode
    resolves to "auto-not-found") for any game not given an explicit
    payload."""

    def __init__(self, payload_by_appid: dict | None = None):
        self._payload_by_appid = payload_by_appid or {}

    def get_json(self, url, headers=None):
        for appid, payload in self._payload_by_appid.items():
            if f"appid={appid}" in url:
                return payload
        return {"appnews": {"newsitems": []}}


_POE1_APPID = 238960
_POE2_APPID = 2694490


def test_compute_meta_trigger_before_range_falls_back_to_cron():
    cfg = {**_GAME_VERSIONS_CFG}
    trigger, active, _detail = _compute_meta_trigger(_LEAGUE_START_CFG, cfg, _FakeSteamHttp(), today=date(2026, 7, 24))
    from apscheduler.triggers.cron import CronTrigger

    assert active is False
    assert isinstance(trigger, CronTrigger)


def test_compute_meta_trigger_on_start_date_is_active():
    from apscheduler.triggers.interval import IntervalTrigger

    cfg = {**_GAME_VERSIONS_CFG}
    trigger, active, detail = _compute_meta_trigger(_LEAGUE_START_CFG, cfg, _FakeSteamHttp(), today=date(2026, 7, 25))
    assert active is True
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.total_seconds() == 6 * 3600  # 24h / 4 runs
    assert detail["poe1"] == {"source": "manual", "start_date": "2026-07-25", "end_date": "2026-08-15", "in_range": True}
    assert detail["poe2"]["source"] == "auto-not-found"
    assert detail["poe2"]["in_range"] is False


def test_compute_meta_trigger_on_end_date_is_still_active():
    cfg = {**_GAME_VERSIONS_CFG}
    _trigger, active, _detail = _compute_meta_trigger(_LEAGUE_START_CFG, cfg, _FakeSteamHttp(), today=date(2026, 8, 15))
    assert active is True


def test_compute_meta_trigger_after_range_falls_back_to_cron():
    from apscheduler.triggers.cron import CronTrigger

    cfg = {**_GAME_VERSIONS_CFG}
    trigger, active, _detail = _compute_meta_trigger(_LEAGUE_START_CFG, cfg, _FakeSteamHttp(), today=date(2026, 8, 16))
    assert active is False
    assert isinstance(trigger, CronTrigger)


def test_compute_meta_trigger_no_league_start_mode_uses_manual_knob():
    """No league_start_mode configured at all -- must fall back to the
    existing manual meta_runs_per_day_league_start knob unchanged, same as
    before this feature existed."""
    from apscheduler.triggers.interval import IntervalTrigger

    schedule_cfg = {"meta_cron_hour_thailand": 2, "meta_runs_per_day_league_start": 4}
    trigger, active, detail = _compute_meta_trigger(schedule_cfg, _GAME_VERSIONS_CFG, _FakeSteamHttp(), today=date(2026, 7, 25))
    assert active is False
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.total_seconds() == 6 * 3600
    assert detail["poe1"]["source"] == "auto-not-found"
    assert detail["poe2"]["source"] == "auto-not-found"


def test_compute_meta_trigger_manual_wins_over_auto_even_when_auto_would_also_resolve(tmp_path):
    """poe1 is manually configured; even feeding it Steam data that WOULD
    auto-resolve to something must not override the manual entry."""
    payload = json.loads((SAMPLES_ROOT / "steam_news_poe1_wide.json").read_text(encoding="utf-8"))
    http = _FakeSteamHttp({_POE1_APPID: payload})
    cfg = {**_GAME_VERSIONS_CFG, "game_versions": ["poe1"]}

    _trigger, _active, detail = _compute_meta_trigger(_LEAGUE_START_CFG, cfg, http, today=date(2026, 8, 1))
    assert detail["poe1"]["source"] == "manual"
    assert detail["poe1"]["start_date"] == "2026-07-25"  # the manual date, not the auto-detected July 24


def test_compute_meta_trigger_union_active_if_either_game_in_range():
    """poe1's manual window doesn't cover today, but poe2 auto-detects a
    currently-in-range window from real sample data -- overall must still
    be active (union across games, same precedent the manual knob already
    established: one meta invocation benefits both games)."""
    payload = json.loads((SAMPLES_ROOT / "steam_news_poe2.json").read_text(encoding="utf-8"))
    http = _FakeSteamHttp({_POE2_APPID: payload})
    cfg = {**_GAME_VERSIONS_CFG}

    # poe2's real sample data auto-detects a launch on 2026-05-29; pick a
    # today inside that derived window but outside poe1's manual window.
    _trigger, active, detail = _compute_meta_trigger(_LEAGUE_START_CFG, cfg, http, today=date(2026, 6, 5))
    assert detail["poe1"]["in_range"] is False
    assert detail["poe2"]["source"] == "auto"
    assert detail["poe2"]["in_range"] is True
    assert active is True


def test_compute_meta_trigger_auto_stale_detection_does_not_activate():
    """poe2's real sample data's most recent 'launch on <date>' match is
    long past its derived window by 2026-08-01 -- must resolve to
    auto-stale (not plain "auto") and never activate."""
    payload = json.loads((SAMPLES_ROOT / "steam_news_poe2.json").read_text(encoding="utf-8"))
    http = _FakeSteamHttp({_POE2_APPID: payload})
    cfg = {**_GAME_VERSIONS_CFG, "game_versions": ["poe2"]}

    _trigger, active, detail = _compute_meta_trigger({"league_start_mode": {"meta_runs_per_day": 4}}, cfg, http, today=date(2026, 8, 1))
    assert detail["poe2"]["source"] == "auto-stale"
    assert detail["poe2"]["in_range"] is False
    assert active is False


def test_compute_meta_trigger_malformed_manual_falls_back_to_auto_for_that_game(caplog):
    schedule_cfg = {
        "meta_cron_hour_thailand": 2,
        "league_start_mode": {
            "meta_runs_per_day": 4,
            "poe1": {"start_date": "2026-08-15", "end_date": "2026-07-25"},  # start after end
        },
    }
    from apscheduler.triggers.cron import CronTrigger

    cfg = {**_GAME_VERSIONS_CFG, "game_versions": ["poe1"]}
    trigger, active, detail = _compute_meta_trigger(schedule_cfg, cfg, _FakeSteamHttp(), today=date(2026, 8, 1))
    assert active is False
    assert isinstance(trigger, CronTrigger)
    assert detail["poe1"]["source"] == "auto-not-found"  # fell back, no Steam data supplied here
    assert "malformed" in caplog.text.lower()


def test_compute_meta_trigger_malformed_missing_key_falls_back(caplog):
    schedule_cfg = {"league_start_mode": {"poe1": {"start_date": "2026-07-25"}}}  # missing end_date
    from apscheduler.triggers.cron import CronTrigger

    cfg = {**_GAME_VERSIONS_CFG, "game_versions": ["poe1"]}
    trigger, active, _detail = _compute_meta_trigger(schedule_cfg, cfg, _FakeSteamHttp(), today=date(2026, 8, 1))
    assert active is False
    assert isinstance(trigger, CronTrigger)
    assert "malformed" in caplog.text.lower()


def test_run_scheduler_uses_league_start_mode_when_today_in_range(tmp_path):
    from apscheduler.triggers.interval import IntervalTrigger

    cfg = {"firebase": {"bucket": "test-bucket"}, "schedule": _LEAGUE_START_CFG, "game_versions": ["poe1"]}
    fake_scheduler = MagicMock()
    with patch("src.main.BlockingScheduler", return_value=fake_scheduler), \
         patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, [])), \
         patch("src.main._build_http_client", return_value=_FakeSteamHttp()), \
         patch("src.main.datetime") as mock_datetime:
        import datetime as real_datetime_module
        mock_datetime.now.return_value = real_datetime_module.datetime(2026, 8, 1, 12, 0)
        run_scheduler(cfg)

    meta_call = next(c for c in fake_scheduler.add_job.call_args_list if c.kwargs.get("id") == "meta")
    trigger = meta_call.args[1]
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.total_seconds() == 6 * 3600

    # a dedicated hourly checker job must also be registered
    check_call = next(c for c in fake_scheduler.add_job.call_args_list if c.kwargs.get("id") == "league_start_mode_check")
    assert check_call.args[1].interval.total_seconds() == 3600


def test_run_scheduler_writes_league_start_mode_status_on_startup(tmp_path):
    created: list = []
    cfg = {"firebase": {"bucket": "test-bucket"}, "schedule": _LEAGUE_START_CFG, "game_versions": ["poe1"]}
    fake_scheduler = MagicMock()
    with patch("src.main.BlockingScheduler", return_value=fake_scheduler), \
         patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created)), \
         patch("src.main._build_http_client", return_value=_FakeSteamHttp()):
        run_scheduler(cfg)  # real "today" -- before 2026-07-25 as of this test suite's writing

    status = created[-1].fetch_previous("status.json")
    assert status["league_start_mode"] == "inactive"
    assert status["league_start_mode_detail"]["poe1"]["source"] == "manual"


def test_check_league_start_mode_reschedules_and_updates_status_on_state_change(tmp_path):
    import src.main as main_module

    main_module._league_start_mode_active = False
    fake_scheduler = MagicMock()
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()
    cfg = {"schedule": _LEAGUE_START_CFG, "game_versions": ["poe1"]}

    _check_league_start_mode(fake_scheduler, cfg, _FakeSteamHttp(), publisher, today=date(2026, 8, 1))

    fake_scheduler.reschedule_job.assert_called_once()
    assert fake_scheduler.reschedule_job.call_args.args[0] == "meta"
    status = publisher.fetch_previous("status.json")
    assert status["league_start_mode"] == "active"
    assert status["league_start_mode_detail"]["poe1"]["source"] == "manual"
    assert main_module._league_start_mode_active is True


def test_check_league_start_mode_no_reschedule_when_state_unchanged(tmp_path):
    import src.main as main_module

    main_module._league_start_mode_active = True  # already active
    fake_scheduler = MagicMock()
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()
    cfg = {"schedule": _LEAGUE_START_CFG, "game_versions": ["poe1"]}

    _check_league_start_mode(fake_scheduler, cfg, _FakeSteamHttp(), publisher, today=date(2026, 8, 2))  # still in range

    fake_scheduler.reschedule_job.assert_not_called()


def test_check_league_start_mode_does_not_touch_job_lock_or_run_meta(tmp_path):
    """A meta run stuck mid-execution across a mode-switch boundary is safe:
    rescheduling only changes the job's *next* fire-time computation, it
    never invokes the job function itself or touches job_lock -- the
    existing lock (CLAUDE.md's 2026-07-15 incident) is untouched by this
    entirely separate mechanism."""
    import src.main as main_module

    main_module._league_start_mode_active = False
    fake_scheduler = MagicMock()
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()
    cfg = {"schedule": _LEAGUE_START_CFG, "game_versions": ["poe1"]}

    lock_dir = tmp_path / "locks"
    with patch("src.main.job_lock.DEFAULT_LOCK_DIR", lock_dir), \
         patch("src.main.run_meta_once") as fake_run_meta_once:
        with job_lock.acquire("meta", lock_dir=lock_dir):
            _check_league_start_mode(fake_scheduler, cfg, _FakeSteamHttp(), publisher, today=date(2026, 8, 1))
            # the lock is still held after the check -- reschedule didn't
            # release it, touch it, or try to run the job itself
            assert job_lock.read_lock("meta", lock_dir=lock_dir) is not None
        fake_run_meta_once.assert_not_called()

    fake_scheduler.reschedule_job.assert_called_once()


class _FakeStorageClient:
    """Minimal in-memory stand-in for google.cloud.storage.Client, enough
    for PricesPublisher.fetch_previous/publish to round-trip through."""

    def __init__(self):
        self._blobs: dict[str, str] = {}

    def bucket(self, name):
        return _FakeBucket(self._blobs)


class _FakeBucket:
    def __init__(self, blobs):
        self._blobs = blobs

    def blob(self, path):
        return _FakeBlob(self._blobs, path)

    def list_blobs(self, prefix=""):
        return [_FakeListedBlob(self._blobs, name) for name in list(self._blobs.keys()) if name.startswith(prefix)]


class _FakeListedBlob:
    def __init__(self, blobs, name):
        self._blobs = blobs
        self.name = name

    def delete(self):
        self._blobs.pop(self.name, None)


class _FakeBlob:
    def __init__(self, blobs, path):
        self._blobs = blobs
        self._path = path
        self.cache_control = None

    def exists(self):
        return self._path in self._blobs

    def download_as_text(self):
        return self._blobs[self._path]

    def upload_from_string(self, payload, content_type=None):
        self._blobs[self._path] = payload
