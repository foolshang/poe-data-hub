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
    run_prices_once,
)
from src.storage.publisher import PricesPublisher


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


def test_update_meta_status_tracks_rolling_history(tmp_path):
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()

    data = {"generated_at": "2026-07-10T00:00:00Z", "league": "Runes of Aldur"}
    for pct in [2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7]:
        _update_meta_status(
            publisher, data=data, end_at=None,
            summary={"unmapped_mod_pct": pct, "stats_cache_fetched_at": time.time()},
        )

    status = publisher.fetch_previous("status.json")
    assert len(status["meta_unmapped_history"]) == UNMAPPED_HISTORY_LEN
    # oldest entry (2.0) should have been dropped once the window filled past 7
    assert 2.0 not in status["meta_unmapped_history"]
    assert status["stats_cache_stale"] is False


def test_update_meta_status_flags_stale_and_logs_error(tmp_path):
    publisher = PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)
    publisher._client = _FakeStorageClient()

    data = {"generated_at": "2026-07-10T00:00:00Z", "league": "Runes of Aldur"}
    old_ts = time.time() - 45 * 86400
    _update_meta_status(
        publisher, data=data, end_at=None,
        summary={"unmapped_mod_pct": 1.0, "stats_cache_fetched_at": old_ts},
    )

    status = publisher.fetch_previous("status.json")
    assert status["stats_cache_stale"] is True
    assert any("stats_cache_stale" in e["detail"] for e in status["errors"])


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


def _fake_prices_publisher_factory(tmp_path, created: list):
    def factory(*, bucket_name, project_id=None):
        p = PricesPublisher(bucket_name=bucket_name, local_cache_dir=tmp_path)
        p._client = _FakeStorageClient()
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
            LeagueResult(main="Runes of Aldur", events=["Some Event"])
            if game == "poe2"
            else LeagueResult(main="Mirage", events=[])
        )
        return d

    created_publishers: list = []
    with patch("src.main.PricesPublisher", new=_fake_prices_publisher_factory(tmp_path, created_publishers)), \
         patch("src.main.NinjaPricesFetcher", return_value=fake_ninja_instance) as NinjaCls, \
         patch("src.main.ScoutPricesFetcher", return_value=fake_scout_instance) as ScoutCls, \
         patch("src.main.LeagueDetector", side_effect=detector_factory):
        run_prices_once(cfg)

    # ScoutPricesFetcher (poe2scout.com, PoE2-only) must be constructed only
    # for poe2's main + event league, never for poe1.
    assert ScoutCls.call_count == 2

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

    status = publisher.fetch_previous("status.json")
    assert status["prices"]["poe2"]["league"] == "Runes of Aldur"
    assert status["prices"]["poe1"]["league"] == "Mirage"


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
