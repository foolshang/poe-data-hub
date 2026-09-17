"""Unit tests for src/processing/archive_export.py (skill-meta phase 5)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.processing.archive_export import build_league_archive, export_league_archive
from src.storage.local_db import LocalDb


def _skill_row(**overrides):
    row = {
        "slot": "ring", "main_skill": "Fireball", "level_bracket": "90-100", "archetype": "spell-fire",
        "stat_id": "explicit.stat_life", "mod_kind": "explicit", "count": 8, "population": 10,
        "usage_pct": 80.0, "rank_in_bracket": 1, "value_min": 40.0, "value_max": 115.0,
    }
    row.update(overrides)
    return row


def _mod_row(**overrides):
    row = {
        "slot": "ring", "archetype": "spell-fire", "stat_id": "explicit.stat_life", "mod_kind": "explicit",
        "count": 8, "usage_pct": 80.0, "rank_in_slot": 1, "value_min": 40.0, "value_max": 115.0,
    }
    row.update(overrides)
    return row


def test_build_league_archive_basic_shape(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [_skill_row()])
    db.insert_mod_counts(snap_id, [_mod_row()])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert archive["schema_version"] == 1
    assert archive["game"] == "poe2"
    assert archive["league"] == "L"
    assert archive["league_slug"] == "l"
    assert archive["age_buckets"] == ["1-21", "22-60", "61+", "unknown"]
    assert archive["passives"] is None
    assert archive["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["1-21"] == [
        {"stat_id": "explicit.stat_life", "mod_kind": "explicit", "count": 8,
         "value_min": 40.0, "value_max": 115.0, "population": 10, "usage_pct": 80.0}
    ]


def test_mod_counts_archive_has_no_population_or_usage_pct(tmp_path):
    """mod_counts has no population column at all (unlike skill_mod_counts)
    -- archived rows must not carry a fabricated usage_pct."""
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_mod_counts(snap_id, [_mod_row()])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    row = archive["mod_counts"]["ring"]["spell-fire"]["1-21"][0]
    assert row == {"stat_id": "explicit.stat_life", "mod_kind": "explicit", "count": 8, "value_min": 40.0, "value_max": 115.0}
    assert "population" not in row
    assert "usage_pct" not in row


def test_snapshots_in_same_bucket_sum_counts_and_dedupe_population(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap1 = db.insert_snapshot(game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    snap2 = db.insert_snapshot(game="poe2", league="L", days_since_league_start=10, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap1, "poe2", 3, [_skill_row(count=8, population=10)])
    db.insert_skill_mod_counts(snap2, "poe2", 10, [_skill_row(count=4, population=6)])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    row = archive["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["1-21"][0]
    assert row["count"] == 12  # 8 + 4, summed
    assert row["population"] == 16  # 10 + 6, both snapshots counted (distinct groups)
    assert row["usage_pct"] == round(12 / 16 * 100, 2)


def test_snapshots_in_different_age_buckets_kept_separate(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_early = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    snap_late = db.insert_snapshot(game="poe2", league="L", days_since_league_start=70, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_early, "poe2", 5, [_skill_row(count=8, population=10)])
    db.insert_skill_mod_counts(snap_late, "poe2", 70, [_skill_row(count=3, population=5)])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    buckets = archive["skill_mod_counts"]["ring"]["Fireball"]["90-100"]
    assert set(buckets.keys()) == {"1-21", "61+"}
    assert buckets["1-21"][0]["count"] == 8
    assert buckets["61+"][0]["count"] == 3


def test_null_days_since_league_start_lands_in_unknown_bucket_not_dropped(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=None, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", None, [_skill_row()])
    db.insert_mod_counts(snap_id, [_mod_row()])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert "unknown" in archive["skill_mod_counts"]["ring"]["Fireball"]["90-100"]
    assert "unknown" in archive["mod_counts"]["ring"]["spell-fire"]


def test_boundary_days_land_in_expected_buckets(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    for days in (1, 21, 22, 60, 61):
        snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=days, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
        db.insert_mod_counts(snap, [_mod_row(stat_id=f"stat_{days}")])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    by_stat = {}
    for bucket, rows in archive["mod_counts"]["ring"]["spell-fire"].items():
        for r in rows:
            by_stat[r["stat_id"]] = bucket
    assert by_stat["stat_1"] == "1-21"
    assert by_stat["stat_21"] == "1-21"
    assert by_stat["stat_22"] == "22-60"
    assert by_stat["stat_60"] == "22-60"
    assert by_stat["stat_61"] == "61+"


def test_does_not_leak_other_games_or_leagues(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_a = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_a, "poe2", 5, [_skill_row()])
    snap_b = db.insert_snapshot(game="poe2", league="OtherLeague", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_b, "poe2", 5, [_skill_row(slot="amulet")])
    snap_c = db.insert_snapshot(game="poe1", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_c, "poe1", 5, [_skill_row(slot="belt")])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert list(archive["skill_mod_counts"].keys()) == ["ring"]


def test_export_league_archive_writes_file_and_reports_size(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="Runes of Aldur", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [_skill_row()])

    archive_dir = tmp_path / "archive"
    path = export_league_archive(db, "poe2", "Runes of Aldur", archive_dir=archive_dir)
    db.close()

    assert path == archive_dir / "poe2-runes-of-aldur.json"
    assert path.exists()
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["league"] == "Runes of Aldur"


def test_export_league_archive_publishes_to_firebase_when_publisher_given(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    publisher = MagicMock()

    export_league_archive(db, "poe2", "L", archive_dir=tmp_path / "archive", publisher=publisher)
    db.close()

    publisher.publish.assert_called_once()
    args, kwargs = publisher.publish.call_args
    assert args[1] == "archive/poe2-l.json"


def test_export_league_archive_publish_failure_does_not_raise(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    publisher = MagicMock()
    publisher.publish.side_effect = RuntimeError("simulated Firebase outage")

    path = export_league_archive(db, "poe2", "L", archive_dir=tmp_path / "archive", publisher=publisher)
    db.close()

    assert path.exists()  # VM copy still written despite the publish failure
