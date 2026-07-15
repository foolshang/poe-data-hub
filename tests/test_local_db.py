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
