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
    assert archive["passives"] == {}  # no keypassive_counts rows inserted in this test
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


def _keypassive_row(**overrides):
    row = {
        "main_skill": "Fireball", "level_bracket": "90-100", "archetype": "spell-fire",
        "keypassive": "Dance with Death", "count": 8, "population": 10, "usage_pct": 80.0, "rank_in_bracket": 1,
    }
    row.update(overrides)
    return row


def test_passive_archive_basic_shape(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_keypassive_counts(snap_id, "poe2", 5, [_keypassive_row()])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert archive["passives"]["Fireball"]["90-100"]["1-21"] == [
        {"keypassive": "Dance with Death", "count": 8, "population": 10, "usage_pct": 80.0}
    ]


def test_passive_archive_sums_counts_and_dedupes_population_across_snapshots(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap1 = db.insert_snapshot(game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    snap2 = db.insert_snapshot(game="poe2", league="L", days_since_league_start=10, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_keypassive_counts(snap1, "poe2", 3, [_keypassive_row(count=8, population=10)])
    db.insert_keypassive_counts(snap2, "poe2", 10, [_keypassive_row(count=4, population=6)])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    row = archive["passives"]["Fireball"]["90-100"]["1-21"][0]
    assert row["count"] == 12
    assert row["population"] == 16
    assert row["usage_pct"] == round(12 / 16 * 100, 2)


def test_passive_archive_no_data_is_empty_dict_not_null(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert archive["passives"] == {}


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


# --- Skill-centric collection (skill-meta phase 22) -------------------------

from src.storage.local_db import COLLECTION_METHOD_LEGACY, COLLECTION_METHOD_SKILL  # noqa: E402


def _stat_row(**overrides):
    row = {
        "level_bracket": "90-100", "slot": "ring", "stat_id": "explicit.stat_life", "mod_kind": "explicit",
        "count": 8, "population": 10, "slot_holders": 9, "usage_pct": 80.0, "value_min": 40.0, "value_max": 115.0,
    }
    row.update(overrides)
    return row


def _pop_row(**overrides):
    row = {"level_bracket": "90-100", "population": 10, "facet_count": 500}
    row.update(overrides)
    return row


def _skill_passive_row(**overrides):
    row = {"level_bracket": "90-100", "keypassive": "Dance with Death", "count": 8, "population": 10, "usage_pct": 80.0}
    row.update(overrides)
    return row


def test_legacy_archive_sections_ignore_skill_centric_snapshots(tmp_path):
    """Defensive: even if a skill_top100_v1 snapshot somehow had rows in the
    OLD tables (it shouldn't -- the skill-centric job never writes there),
    the legacy sections must not include them."""
    db = LocalDb(tmp_path / "test.sqlite3")
    skill_snap = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    db.insert_skill_mod_counts(skill_snap, "poe2", 5, [_skill_row()])
    db.insert_keypassive_counts(skill_snap, "poe2", 5, [_keypassive_row()])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert archive["skill_mod_counts"] == {}
    assert archive["passives"] == {}


def test_mod_counts_archive_spans_both_collection_methods(tmp_path):
    """mod_counts has no main_skill/skill involved -- it keeps archiving
    regardless of collection_method, unlike skill_mod_counts/passives."""
    db = LocalDb(tmp_path / "test.sqlite3")
    legacy_snap = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0,
    )
    skill_snap = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=6, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    db.insert_mod_counts(legacy_snap, [_mod_row(count=8)])
    db.insert_mod_counts(skill_snap, [_mod_row(count=3)])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert archive["mod_counts"]["ring"]["spell-fire"]["1-21"][0]["count"] == 11


def test_skills_archive_basic_shape(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    db.replace_skill_counts_for_snapshot(
        snap, "poe2", 5, "main", "Fireball", [_stat_row()], [_skill_passive_row()], [_pop_row()],
    )

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    leaf = archive["skills"]["main"]["Fireball"]["90-100"]["1-21"]
    assert leaf["population"] == 10
    assert leaf["mods"]["ring"] == [
        {"stat_id": "explicit.stat_life", "mod_kind": "explicit", "count": 8,
         "value_min": 40.0, "value_max": 115.0, "population": 10, "usage_pct": 80.0}
    ]
    assert leaf["passives"] == [
        {"keypassive": "Dance with Death", "count": 8, "population": 10, "usage_pct": 80.0}
    ]


def test_skills_archive_sums_across_snapshots_and_dedupes_population(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap1 = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    snap2 = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=10, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    db.replace_skill_counts_for_snapshot(snap1, "poe2", 3, "main", "Fireball", [_stat_row(count=8, population=10)], [], [_pop_row(population=10)])
    db.replace_skill_counts_for_snapshot(snap2, "poe2", 10, "main", "Fireball", [_stat_row(count=4, population=6)], [], [_pop_row(population=6)])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    row = archive["skills"]["main"]["Fireball"]["90-100"]["1-21"]["mods"]["ring"][0]
    assert row["count"] == 12
    assert row["population"] == 16
    assert row["usage_pct"] == round(12 / 16 * 100, 2)


def test_skills_archive_keeps_main_and_spirit_separate(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    db.replace_skill_counts_for_snapshot(snap, "poe2", 5, "main", "Herald of Ice", [_stat_row(count=3)], [], [_pop_row()])
    db.replace_skill_counts_for_snapshot(snap, "poe2", 5, "spirit", "Herald of Ice", [_stat_row(count=7)], [], [_pop_row()])

    archive = build_league_archive(db, "poe2", "L")
    db.close()

    assert archive["skills"]["main"]["Herald of Ice"]["90-100"]["1-21"]["mods"]["ring"][0]["count"] == 3
    assert archive["skills"]["spirit"]["Herald of Ice"]["90-100"]["1-21"]["mods"]["ring"][0]["count"] == 7


def test_skills_archive_no_data_is_empty_dict(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    archive = build_league_archive(db, "poe2", "L")
    db.close()
    assert archive["skills"] == {}


# --- Archive never shrinks: retention-safe merge (approved 2026-09-22) -----

from src.processing.archive_export import ArchiveRegressionError, build_league_archive  # noqa: E402
import pytest  # noqa: E402


def test_freeze_keeps_early_league_data_after_its_raw_rows_age_out(tmp_path):
    """The exact scenario that motivated this: retention deletes an early
    snapshot's raw rows once it's older than the retention window; a later
    re-export must not silently lose that already-archived early-league data."""
    db = LocalDb(tmp_path / "test.sqlite3")
    early_snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    late_snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=70, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(early_snap, "poe2", 3, [_skill_row(count=8, population=10)])
    db.insert_skill_mod_counts(late_snap, "poe2", 70, [_skill_row(count=3, population=5)])

    first = build_league_archive(db, "poe2", "L")
    assert first["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["1-21"][0]["count"] == 8
    assert first["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["61+"][0]["count"] == 3

    # simulate retention: the early snapshot's raw rows are gone
    db._conn.execute("DELETE FROM skill_mod_counts WHERE snapshot_id = ?", (early_snap,))
    db._conn.commit()

    second = build_league_archive(db, "poe2", "L", previous=first)
    db.close()

    # early-league bucket frozen from `first`, unaffected by the deletion
    assert second["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["1-21"][0]["count"] == 8
    # the still-live late bucket is unaffected either way
    assert second["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["61+"][0]["count"] == 3


def test_export_league_archive_freezes_across_a_real_file_round_trip(tmp_path):
    """Same scenario as above but through the real file-reading path
    (`export_league_archive` itself), the way retention cleanup + the weekly
    archive job actually interact in production."""
    db = LocalDb(tmp_path / "test.sqlite3")
    early_snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(early_snap, "poe2", 3, [_skill_row(count=8, population=10)])
    archive_dir = tmp_path / "archive"

    export_league_archive(db, "poe2", "L", archive_dir=archive_dir)

    db._conn.execute("DELETE FROM skill_mod_counts WHERE snapshot_id = ?", (early_snap,))
    db._conn.commit()
    path = export_league_archive(db, "poe2", "L", archive_dir=archive_dir)
    db.close()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["1-21"][0]["count"] == 8


def test_a_partial_regression_raises_and_never_writes(tmp_path):
    """Not a clean age-out (the bucket didn't go empty, it just lost some but
    not all of its data) -- this must be treated as a bug and refused, not
    silently accepted as a smaller-but-real number."""
    db = LocalDb(tmp_path / "test.sqlite3")
    snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap, "poe2", 5, [_skill_row(count=8, population=10)])
    archive_dir = tmp_path / "archive"

    export_league_archive(db, "poe2", "L", archive_dir=archive_dir)
    path = archive_dir / "poe2-l.json"
    before = path.read_text(encoding="utf-8")

    # shrink the count in place -- not a clean deletion, a partial loss
    db._conn.execute("UPDATE skill_mod_counts SET count = 2 WHERE snapshot_id = ?", (snap,))
    db._conn.commit()

    with pytest.raises(ArchiveRegressionError, match="archive regression"):
        export_league_archive(db, "poe2", "L", archive_dir=archive_dir)
    db.close()

    assert path.read_text(encoding="utf-8") == before  # untouched -- refused, not overwritten


def test_a_key_entirely_missing_from_fresh_is_carried_forward_wholesale(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap, "poe2", 3, [_skill_row(slot="ring", count=8, population=10)])
    first = build_league_archive(db, "poe2", "L")

    db._conn.execute("DELETE FROM skill_mod_counts WHERE slot = 'ring'")
    db._conn.commit()
    second = build_league_archive(db, "poe2", "L", previous=first)
    db.close()

    assert second["skill_mod_counts"]["ring"] == first["skill_mod_counts"]["ring"]


def test_growth_within_a_still_open_bucket_is_kept_not_frozen(tmp_path):
    """A bucket that's still accumulating (more snapshots keep landing in it)
    must use the fresh, bigger total -- freezing must not kick in just
    because a previous archive exists."""
    db = LocalDb(tmp_path / "test.sqlite3")
    snap1 = db.insert_snapshot(game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap1, "poe2", 3, [_skill_row(count=8, population=10)])
    first = build_league_archive(db, "poe2", "L")

    snap2 = db.insert_snapshot(game="poe2", league="L", days_since_league_start=4, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap2, "poe2", 4, [_skill_row(count=5, population=6)])
    second = build_league_archive(db, "poe2", "L", previous=first)
    db.close()

    assert second["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["1-21"][0]["count"] == 13  # 8+5, grown, not frozen


def test_skills_dimension_population_freezes_the_same_way(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    early = db.insert_snapshot(
        game="poe2", league="L", days_since_league_start=3, sample_total=1, unique_characters_fetched=1,
        fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL,
    )
    db.replace_skill_counts_for_snapshot(early, "poe2", 3, "main", "Fireball", [_stat_row()], [], [_pop_row()])
    first = build_league_archive(db, "poe2", "L")
    assert first["skills"]["main"]["Fireball"]["90-100"]["1-21"]["population"] == 10

    db._conn.execute("DELETE FROM skill_stat_counts WHERE snapshot_id = ?", (early,))
    db._conn.execute("DELETE FROM skill_populations WHERE snapshot_id = ?", (early,))
    db._conn.commit()
    second = build_league_archive(db, "poe2", "L", previous=first)
    db.close()

    assert second["skills"]["main"]["Fireball"]["90-100"]["1-21"]["population"] == 10
    assert second["skills"]["main"]["Fireball"]["90-100"]["1-21"]["mods"]["ring"][0]["count"] == 8


def test_no_previous_archive_never_raises_and_behaves_like_before(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap, "poe2", 5, [_skill_row()])
    archive = build_league_archive(db, "poe2", "L", previous=None)
    db.close()
    assert archive["skill_mod_counts"]["ring"]["Fireball"]["90-100"]["1-21"][0]["count"] == 8


def test_previous_archive_missing_a_dimension_key_entirely_does_not_crash(tmp_path):
    """An archive written before the `skills` key existed at all (schema
    grew additively) -- must not error just because `previous` lacks it."""
    db = LocalDb(tmp_path / "test.sqlite3")
    snap = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap, "poe2", 5, [_skill_row()])
    old_shape_previous = {"mod_counts": {}, "skill_mod_counts": {}, "passives": {}}  # no "skills" key

    archive = build_league_archive(db, "poe2", "L", previous=old_shape_previous)
    db.close()
    assert archive["skills"] == {}
