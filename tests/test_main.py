"""Unit tests for src/main.py's status.json staleness monitoring
(_check_stats_cache_stale), the meta status update flow, the Layer-1
healthchecks.io dead-man-switch ping helper, and (phase 7) the multi-game /
multi-league prices job (run_prices_once, _update_status)."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from src.fetchers.league_detector import LeagueResult
from src.main import (
    UNMAPPED_HISTORY_LEN,
    _check_stats_cache_stale,
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

    def fake_fetch_news(http, game):
        return [{"id": f"{game}-1", "title": f"{game} news", "category": "news", "link": "https://www.pathofexile.com/x", "link_is_ggg": True, "content": "", "published": "2026-07-12T00:00:00Z"}]

    def fake_fetch_deals(http):
        return [{"item_name": "Premium Quad Stash Tab", "link": "x", "discount_percent": 20, "points_price": 120, "ends_in": "Ends in 1 day"}]

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main.news.fetch_news", side_effect=fake_fetch_news), \
         patch("src.main.deals.fetch_deals", side_effect=fake_fetch_deals):
        result1 = run_news_once(cfg)
        result2 = run_news_once(cfg)  # second run: mtx first_seen_at must carry over

    assert result1["ok"] is True
    assert result2["ok"] is True

    publisher = created_publishers[-1]
    poe2_news = publisher.fetch_previous("poe2/news/latest.json")
    assert poe2_news["entries"][0]["id"] == "poe2-1"
    assert poe2_news["sources"] == {"steam": "ok"}
    assert not poe2_news["entries"][0]["link"].startswith("https://th.pathofexile.com")

    poe1_news = publisher.fetch_previous("poe1/news/latest.json")
    assert poe1_news["entries"][0]["id"] == "poe1-1"

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

    def fake_fetch_news_ok(http, game):
        return [{"id": "1", "title": "first run", "category": "news", "link": "x", "link_is_ggg": False, "content": "", "published": "2026-07-12T00:00:00Z"}]

    def fake_fetch_news_fail(http, game):
        raise RuntimeError("steam down")

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main.deals.fetch_deals", return_value=[]):
        with patch("src.main.news.fetch_news", side_effect=fake_fetch_news_ok):
            run_news_once(cfg)
        with patch("src.main.news.fetch_news", side_effect=fake_fetch_news_fail):
            result = run_news_once(cfg)

    assert result["ok"] is False
    # each run_news_once call constructs its own PricesPublisher -- read back
    # from the second call's instance, not the first, to see what it actually
    # published (they share the same on-disk cache dir/Firebase bucket in
    # production; the fake in-memory client here is per-instance only).
    publisher = created_publishers[-1]
    poe2_news = publisher.fetch_previous("poe2/news/latest.json")
    assert poe2_news["sources"] == {"steam": "error"}
    assert poe2_news["entries"][0]["id"] == "1"  # stale-while-error reused the first run's entry


def test_run_scheduler_uses_daily_cron_when_meta_runs_per_day_is_one():
    from apscheduler.triggers.cron import CronTrigger

    cfg = {"schedule": {"meta_cron_hour_thailand": 2, "meta_runs_per_day_league_start": 1}}
    fake_scheduler = MagicMock()
    with patch("src.main.BlockingScheduler", return_value=fake_scheduler):
        run_scheduler(cfg)

    meta_call = next(c for c in fake_scheduler.add_job.call_args_list if c.kwargs.get("id") == "meta")
    trigger = meta_call.args[1]
    assert isinstance(trigger, CronTrigger)


def test_run_scheduler_switches_to_evenly_spaced_runs_for_league_start():
    """League-start mode (spec 9): bumping meta_runs_per_day_league_start
    above 1 must switch the meta job from a once-daily cron to N
    evenly-spaced runs/day, via this one config line -- no code change.
    Confirmed here since the pipeline's own SPEC.md relies on this switch
    being ready before the next PoE1 league launch (2026-07-25)."""
    from apscheduler.triggers.interval import IntervalTrigger

    cfg = {"schedule": {"meta_cron_hour_thailand": 2, "meta_runs_per_day_league_start": 4}}
    fake_scheduler = MagicMock()
    with patch("src.main.BlockingScheduler", return_value=fake_scheduler):
        run_scheduler(cfg)

    meta_call = next(c for c in fake_scheduler.add_job.call_args_list if c.kwargs.get("id") == "meta")
    trigger = meta_call.args[1]
    assert isinstance(trigger, IntervalTrigger)
    assert trigger.interval.total_seconds() == 6 * 3600  # 24h / 4 runs


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
