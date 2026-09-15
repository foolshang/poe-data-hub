from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from src.storage.local_db import LocalDb


def test_insert_and_read_snapshot(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(
        game="poe2", league="Runes of Aldur", days_since_league_start=12,
        sample_total=1400, unique_characters_fetched=640, fetch_failed=12,
    )
    assert snap_id == 1
    row = db._conn.execute("SELECT game, league FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    assert row == ("poe2", "Runes of Aldur")
    db.close()


def test_insert_snapshot_records_poe1_game(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(
        game="poe1", league="Mirage", days_since_league_start=5,
        sample_total=800, unique_characters_fetched=400, fetch_failed=2,
    )
    row = db._conn.execute("SELECT game FROM snapshots WHERE id=?", (snap_id,)).fetchone()
    assert row == ("poe1",)
    db.close()


def test_migration_adds_game_column_defaulting_to_poe2(tmp_path):
    """Simulates production: a pre-phase-10 database with real poe2-only
    snapshot rows and no `game` column at all -- the migration must add the
    column and backfill existing rows as 'poe2' without touching their data."""
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            league TEXT NOT NULL,
            days_since_league_start INTEGER,
            sample_total INTEGER NOT NULL,
            unique_characters_fetched INTEGER NOT NULL,
            fetch_failed INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO snapshots (snapshot_date, league, days_since_league_start, sample_total, "
        "unique_characters_fetched, fetch_failed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("2026-07-01", "Runes of Aldur", 10, 1000, 500, 5, "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    db = LocalDb(db_path)
    row = db._conn.execute("SELECT game, league FROM snapshots WHERE league='Runes of Aldur'").fetchone()
    assert row == ("poe2", "Runes of Aldur")  # pre-existing row backfilled correctly
    db.close()


def test_insert_char_item_and_mod_counts(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=1, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_char_item(snap_id, "acc", "Char", 100, "Titan", "attack-melee", {"items": []})
    db.insert_mod_counts(snap_id, [
        {"slot": "ring", "archetype": "all", "stat_id": "explicit.stat_x", "mod_kind": "explicit",
         "count": 67, "usage_pct": 67.0, "rank_in_slot": 1, "value_min": 40, "value_max": 115},
    ])
    row = db._conn.execute("SELECT count(*) FROM char_items WHERE snapshot_id=?", (snap_id,)).fetchone()
    assert row[0] == 1
    row2 = db._conn.execute("SELECT count(*) FROM mod_counts WHERE snapshot_id=?", (snap_id,)).fetchone()
    assert row2[0] == 1
    db.close()


def test_migration_adds_main_skill_column_defaulting_to_null(tmp_path):
    """Simulates production: a pre-skill-meta-phase-2 database with real
    char_items rows and no main_skill column -- must add the column and
    leave existing rows NULL (that skill data is genuinely unrecoverable
    for them, not merely unmigrated -- see CLAUDE.md)."""
    db_path = tmp_path / "legacy.sqlite3"
    db = LocalDb(db_path)  # creates the up-to-date schema first...
    db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE char_items RENAME TO char_items_new")
    conn.execute(
        """CREATE TABLE char_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            account TEXT NOT NULL, name TEXT NOT NULL, level INTEGER, class TEXT,
            archetype TEXT, items_json TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )  # ...then recreate it in the OLD (pre-main_skill) shape to simulate a real legacy DB
    conn.execute("DROP TABLE char_items_new")
    conn.execute(
        "INSERT INTO char_items (snapshot_id, account, name, level, class, archetype, items_json, created_at) "
        "VALUES (1, 'acc', 'Old', 90, 'Titan', 'attack-melee', '{}', '2026-07-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    db2 = LocalDb(db_path)
    row = db2._conn.execute("SELECT main_skill FROM char_items WHERE name='Old'").fetchone()
    assert row == (None,)
    db2.close()


def test_insert_char_item_with_main_skill(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=1, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_char_item(snap_id, "acc", "Char", 92, "Titan", "attack-melee", {"items": []}, main_skill="Ice Nova of Frostbolts")
    row = db._conn.execute("SELECT main_skill FROM char_items WHERE snapshot_id=?", (snap_id,)).fetchone()
    assert row == ("Ice Nova of Frostbolts",)
    db.close()


def _skill_row(**overrides):
    row = {
        "slot": "ring", "main_skill": "Fireball", "level_bracket": "90-100", "archetype": "spell-fire",
        "stat_id": "explicit.stat_life", "mod_kind": "explicit", "count": 8, "population": 10,
        "usage_pct": 80.0, "rank_in_bracket": 1, "value_min": 40.0, "value_max": 115.0,
    }
    row.update(overrides)
    return row


def test_insert_and_query_skill_mod_counts_exact_tier(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [_skill_row()])

    tier, rows = db.query_skill_mod_counts("poe2", "ring", "Fireball", "90-100", min_population=5)
    assert tier == "skill_bracket"
    assert rows == [{"stat_id": "explicit.stat_life", "mod_kind": "explicit", "count": 8,
                      "value_min": 40.0, "value_max": 115.0, "population": 10, "usage_pct": 80.0}]
    db.close()


def test_query_skill_mod_counts_falls_back_through_all_four_tiers(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [
        # Exact skill+bracket cell is thin (population 3, below the min_population=20 asked for below).
        _skill_row(main_skill="Fireball", level_bracket="90-100", archetype="spell-fire", population=3, count=3, stat_id="s1"),
        # Same skill, a DIFFERENT bracket -- widens the "skill" tier's population to 3+9=12, still < 20.
        _skill_row(main_skill="Fireball", level_bracket="75-90", archetype="spell-fire", population=9, count=9, stat_id="s1"),
        # Same archetype, a DIFFERENT skill -- widens the "archetype" tier to 12+50=62, clears 20.
        _skill_row(main_skill="Firestorm", level_bracket="75-90", archetype="spell-fire", population=50, count=10, stat_id="s1"),
    ])

    tier, rows = db.query_skill_mod_counts("poe2", "ring", "Fireball", "90-100", min_population=20)
    assert tier == "archetype"
    row = next(r for r in rows if r["stat_id"] == "s1")
    assert row["population"] == 62
    assert row["count"] == 3 + 9 + 10
    db.close()


def test_query_skill_mod_counts_all_tier_is_the_final_floor(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [_skill_row(population=3, count=3)])

    # Even the broadest tier can't clear an unreasonably high threshold --
    # "all" is still returned (never raises), just under-population.
    tier, rows = db.query_skill_mod_counts("poe2", "ring", "Fireball", "90-100", min_population=999)
    assert tier == "all"
    assert rows[0]["population"] == 3
    db.close()


def test_query_skill_mod_counts_does_not_leak_other_slots_or_games(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [
        _skill_row(slot="ring", population=50, count=50),
        _skill_row(slot="amulet", population=50, count=50),
    ])
    snap_id2 = db.insert_snapshot(game="poe1", league="L2", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id2, "poe1", 5, [_skill_row(slot="ring", population=50, count=50)])

    tier, rows = db.query_skill_mod_counts("poe2", "ring", "Fireball", "90-100", min_population=1)
    assert tier == "skill_bracket"
    assert rows[0]["population"] == 50  # not 100 (amulet) or 150 (poe1 leaked in)
    db.close()


def test_delete_skill_mod_counts_for_snapshot_makes_reprocess_idempotent(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [_skill_row(), _skill_row(stat_id="s2")])
    assert db._conn.execute("SELECT count(*) FROM skill_mod_counts WHERE snapshot_id=?", (snap_id,)).fetchone()[0] == 2

    deleted = db.delete_skill_mod_counts_for_snapshot(snap_id)
    assert deleted == 2
    db.insert_skill_mod_counts(snap_id, "poe2", 5, [_skill_row()])  # re-insert once, replace semantics
    assert db._conn.execute("SELECT count(*) FROM skill_mod_counts WHERE snapshot_id=?", (snap_id,)).fetchone()[0] == 1
    db.close()


def test_iter_char_items_with_main_skill_skips_null_rows(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_char_item(snap_id, "a", "HasSkill", 92, "Titan", "attack-melee", {"items": []}, main_skill="Fireball")
    db.insert_char_item(snap_id, "b", "NoSkill", 92, "Titan", "attack-melee", {"items": []})  # legacy row, no main_skill
    db.commit()

    rows = db.iter_char_items_with_main_skill("poe2")
    assert len(rows) == 1
    char_item_id, returned_snap_id, main_skill, level, items = rows[0]
    assert main_skill == "Fireball"
    assert returned_snap_id == snap_id
    assert level == 92
    assert items == {"items": []}
    db.close()


def test_cleanup_old_char_items_respects_retention(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=1, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_char_item(snap_id, "acc", "Old", 100, "Titan", "attack-melee", {})
    db.commit()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db._conn.execute("UPDATE char_items SET created_at=? WHERE name='Old'", (old_ts,))
    db.insert_char_item(snap_id, "acc", "New", 100, "Titan", "attack-melee", {})
    db.commit()

    deleted = db.cleanup_old_char_items(older_than_days=90)
    assert deleted == 1
    remaining = db._conn.execute("SELECT name FROM char_items").fetchall()
    assert remaining == [("New",)]
    db.close()
