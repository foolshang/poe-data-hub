from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.storage.local_db import LocalDb


def test_insert_and_read_snapshot(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(
        league="Runes of Aldur", days_since_league_start=12,
        sample_total=1400, unique_characters_fetched=640, fetch_failed=12,
    )
    assert snap_id == 1
    db.close()


def test_insert_char_item_and_mod_counts(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(league="L", days_since_league_start=1, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
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
    snap_id = db.insert_snapshot(league="L", days_since_league_start=1, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
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
