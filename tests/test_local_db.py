from __future__ import annotations

import json
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


def test_insert_char_item_with_passive_selection(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=1, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_char_item(snap_id, "acc", "Char", 92, "Titan", "attack-melee", {"items": []}, passive_selection=[331, 535, 2560])
    row = db._conn.execute("SELECT passive_selection_json FROM char_items WHERE snapshot_id=?", (snap_id,)).fetchone()
    assert json.loads(row[0]) == [331, 535, 2560]
    db.close()


def test_insert_char_item_without_passive_selection_stores_null(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=1, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_char_item(snap_id, "acc", "Char", 92, "Titan", "attack-melee", {"items": []})
    row = db._conn.execute("SELECT passive_selection_json FROM char_items WHERE snapshot_id=?", (snap_id,)).fetchone()
    assert row == (None,)
    db.close()


def test_migration_adds_passive_selection_column_defaulting_to_null(tmp_path):
    """Same situation as main_skill's migration above: a pre-phase-3
    database has no passive_selection_json column at all -- existing rows
    backfill to NULL, that data is genuinely unrecoverable for them."""
    db_path = tmp_path / "legacy2.sqlite3"
    db = LocalDb(db_path)
    db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE char_items DROP COLUMN passive_selection_json")
    conn.execute(
        "INSERT INTO char_items (snapshot_id, account, name, level, class, archetype, items_json, created_at) "
        "VALUES (1, 'acc', 'Old', 90, 'Titan', 'attack-melee', '{}', '2026-07-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    db2 = LocalDb(db_path)
    row = db2._conn.execute("SELECT passive_selection_json FROM char_items WHERE name='Old'").fetchone()
    assert row == (None,)
    db2.close()


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


def _keypassive_row(**overrides):
    row = {
        "main_skill": "Fireball", "level_bracket": "90-100", "archetype": "spell-fire",
        "keypassive": "Dance with Death", "count": 8, "population": 10, "usage_pct": 80.0, "rank_in_bracket": 1,
    }
    row.update(overrides)
    return row


def test_insert_and_iter_keypassive_counts_for_league(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_id = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_keypassive_counts(snap_id, "poe2", 5, [_keypassive_row()])

    rows = db.iter_keypassive_counts_for_league("poe2", "L")
    assert rows == [("Fireball", "90-100", "spell-fire", "Dance with Death", 8, 10, snap_id, 5)]
    db.close()


def test_iter_keypassive_counts_does_not_leak_other_leagues_or_games(tmp_path):
    db = LocalDb(tmp_path / "test.sqlite3")
    snap_a = db.insert_snapshot(game="poe2", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_keypassive_counts(snap_a, "poe2", 5, [_keypassive_row()])
    snap_b = db.insert_snapshot(game="poe2", league="Other", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_keypassive_counts(snap_b, "poe2", 5, [_keypassive_row(keypassive="Ancestral Bond")])
    snap_c = db.insert_snapshot(game="poe1", league="L", days_since_league_start=5, sample_total=1, unique_characters_fetched=1, fetch_failed=0)
    db.insert_keypassive_counts(snap_c, "poe1", 5, [_keypassive_row(keypassive="Acrobatics")])

    rows = db.iter_keypassive_counts_for_league("poe2", "L")
    assert len(rows) == 1
    assert rows[0][3] == "Dance with Death"
    db.close()


# --- Skill-centric collection (2026-09-21) -----------------------------------------

import pytest  # noqa: E402

from src.storage.local_db import COLLECTION_METHOD_LEGACY, COLLECTION_METHOD_SKILL  # noqa: E402


def test_migration_marks_every_existing_snapshot_as_legacy_slot_top100(tmp_path):
    """A production DB from before the skill-centric collection: snapshots have
    no collection_method column. After migrating, all existing rows must carry
    'legacy_slot_top100' (their main_skill is a first-gem guess, unreliable),
    while new skill-centric snapshots carry their own marker."""
    db_path = tmp_path / "prod.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_date TEXT NOT NULL, league TEXT NOT NULL,
            days_since_league_start INTEGER, sample_total INTEGER NOT NULL,
            unique_characters_fetched INTEGER NOT NULL, fetch_failed INTEGER NOT NULL, created_at TEXT NOT NULL,
            game TEXT NOT NULL DEFAULT 'poe2')"""
    )
    conn.execute(
        "INSERT INTO snapshots (snapshot_date, league, sample_total, unique_characters_fetched, fetch_failed, created_at, game) "
        "VALUES ('2026-09-16', 'Allflame', 2183, 1334, 0, '2026-09-16T11:14:35Z', 'poe1')"
    )
    conn.commit()
    conn.close()

    db = LocalDb(db_path)
    assert db._conn.execute("SELECT collection_method FROM snapshots WHERE id = 1").fetchone() == (COLLECTION_METHOD_LEGACY,)
    new_id = db.insert_snapshot("poe1", "Allflame", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    default_id = db.insert_snapshot("poe1", "Allflame", 5, 0, 0, 0)  # the legacy job passes nothing
    methods = dict(db._conn.execute("SELECT id, collection_method FROM snapshots").fetchall())
    assert methods == {1: COLLECTION_METHOD_LEGACY, new_id: COLLECTION_METHOD_SKILL, default_id: COLLECTION_METHOD_LEGACY}
    db.close()


def test_char_details_freshness_rules(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    detail = {"account": "a", "name": "N", "level": 99, "items": []}
    db.upsert_char_detail("poe1", "L", detail, list_level=98)

    assert db.get_fresh_char_detail("poe1", "a", "N", "L", 98, 7) == detail       # list row unchanged
    assert db.get_fresh_char_detail("poe1", "a", "N", "L", 99, 7) is None          # list row moved
    assert db.get_fresh_char_detail("poe1", "a", "N", "Other", 98, 7) is None      # other league
    assert db.get_fresh_char_detail("poe2", "a", "N", "L", 98, 7) is None          # other game
    assert db.get_fresh_char_detail("poe1", "ghost", "N", "L", 98, 7) is None

    db._conn.execute("UPDATE char_details SET fetched_at = '2020-01-01T00:00:00Z'")
    assert db.get_fresh_char_detail("poe1", "a", "N", "L", 98, 7) is None          # too old
    assert db.get_char_detail_any("poe1", "a", "N") == detail                      # but still readable for counting
    assert db.cleanup_old_char_details(30) == 1
    assert db.get_char_detail_any("poe1", "a", "N") is None
    db.close()


def test_upsert_char_detail_replaces_the_row(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    db.upsert_char_detail("poe1", "L", {"account": "a", "name": "N", "level": 90}, list_level=90)
    db.upsert_char_detail("poe1", "L", {"account": "a", "name": "N", "level": 91}, list_level=91)
    assert db._conn.execute("SELECT COUNT(*), MAX(level) FROM char_details").fetchone() == (1, 91)
    db.close()


def _new_run(db, league="L", lists=None, game="poe2"):
    snap = db.insert_snapshot(game, league, None, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    lists = lists or [{"skill_type": "main", "skill": "Fireball", "list_name": "Fireball", "facet_count": 500}]
    return snap, db.create_skill_run(game, league, snap, 100_000, {"plan": True}, lists)


def test_resumable_run_lookup_and_abandonment(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    _snap, run_id = _new_run(db)

    found = db.find_resumable_skill_run("poe2", "L", 48)
    assert found["id"] == run_id and found["plan"] == {"plan": True}
    assert db.find_resumable_skill_run("poe1", "L", 48) is None       # other game
    assert db.find_resumable_skill_run("poe2", "Other", 48) is None   # other league...
    assert db._conn.execute("SELECT status FROM skill_runs WHERE id = ?", (run_id,)).fetchone() == ("abandoned",)  # ...abandons it

    _snap2, run2 = _new_run(db)
    db._conn.execute("UPDATE skill_runs SET started_at = '2020-01-01T00:00:00Z' WHERE id = ?", (run2,))
    assert db.find_resumable_skill_run("poe2", "L", 48) is None       # too old to resume
    assert db._conn.execute("SELECT status FROM skill_runs WHERE id = ?", (run2,)).fetchone() == ("abandoned",)

    _snap3, run3 = _new_run(db)
    db.finish_skill_run(run3, "finished", {})
    assert db.find_resumable_skill_run("poe2", "L", 48) is None       # finished runs are never resumed
    db.close()


def test_saving_a_list_is_atomic_and_marks_it_done(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    _snap, run_id = _new_run(db)
    members = [{"account": "a", "name": "A", "level": 99, "class": "Slayer", "keypassives": ("K1",)}]
    db.save_skill_list_members(run_id, "main", "Fireball", "Fireball", 500, members)

    assert db.pending_skill_lists(run_id) == []
    assert db.skill_list_counts(run_id) == {"done": 1}
    assert db.iter_skill_members(run_id, "main", "Fireball") == [("a", "A", 99, "Slayer", ["K1"])]

    # a poisoned member (missing key) must roll the whole list back, not leave it half-saved
    db2 = LocalDb(tmp_path / "t2.sqlite3")
    _snap2, run2 = _new_run(db2)
    with pytest.raises(KeyError):
        db2.save_skill_list_members(run2, "main", "Fireball", "Fireball", 500, [members[0], {"account": "b"}])
    assert db2.pending_skill_lists(run2) != []
    assert db2._conn.execute("SELECT COUNT(*) FROM skill_run_members").fetchone() == (0,)
    db.close()
    db2.close()


def test_a_failed_list_stays_pending_for_the_next_attempt(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    _snap, run_id = _new_run(db)
    db.mark_skill_list_failed(run_id, "main", "Fireball")
    assert db.skill_list_counts(run_id) == {"failed": 1}
    assert [l["list_name"] for l in db.pending_skill_lists(run_id)] == ["Fireball"]
    db.close()


def test_member_keys_and_cut_stats_exclude_cut_classes(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    lists = [
        {"skill_type": "main", "skill": "A", "list_name": "A", "facet_count": 9},
        {"skill_type": "spirit", "skill": "B", "list_name": "B", "facet_count": 8},
    ]
    _snap, run_id = _new_run(db, lists=lists)

    def m(acct, cls, lv=99):
        return {"account": acct, "name": acct.upper(), "level": lv, "class": cls, "keypassives": ()}

    db.save_skill_list_members(run_id, "main", "A", "A", 9, [m("x", "Slayer"), m("y", "Witch")])
    db.save_skill_list_members(run_id, "spirit", "B", "B", 8, [m("x", "Slayer"), m("z", "Witch")])

    assert sorted(db.iter_run_member_keys(run_id, {"Witch"})) == [("x", "X", 99)]
    assert db.member_stats(run_id, {"Witch"}) == {"member_rows": 4, "cut_rows": 2, "unique_chars": 3, "unique_chars_cut": 2}
    assert db.run_skill_groups(run_id) == [("main", "A"), ("spirit", "B")]
    db.close()


def test_replace_skill_counts_is_idempotent_per_group(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    snap, _run = _new_run(db)
    stat = {"level_bracket": "90-100", "slot": "ring", "stat_id": "s1", "mod_kind": "explicit", "count": 2,
            "population": 4, "slot_holders": 3, "usage_pct": 50.0, "value_min": 1.0, "value_max": 2.0}
    passive = {"level_bracket": "90-100", "keypassive": "K", "count": 1, "population": 4, "usage_pct": 25.0}
    pop = {"level_bracket": "90-100", "population": 4, "facet_count": 500}
    for _ in range(2):  # twice: a recount must replace, never accumulate
        db.replace_skill_counts_for_snapshot(snap, "poe2", 9, "main", "Fireball", [stat], [passive], [pop])
    db.replace_skill_counts_for_snapshot(snap, "poe2", 9, "spirit", "Fireball", [stat], [passive], [pop])

    counts = {t: db._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("skill_stat_counts", "skill_passive_counts", "skill_populations")}
    assert counts == {"skill_stat_counts": 2, "skill_passive_counts": 2, "skill_populations": 2}  # main + spirit, once each
    assert db._conn.execute("SELECT days_since_league_start FROM skill_stat_counts").fetchone() == (9,)
    db.close()


# --- Skill-centric publish queries (skill-meta phase 22) --------------------

def test_latest_finished_skill_run_ignores_running_and_other_games(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    snap1 = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    run1 = db.create_skill_run("poe2", "L", snap1, 1000, {}, [])
    db.finish_skill_run(run1, "finished", {})

    snap2 = db.insert_snapshot("poe2", "L", 9, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    run2 = db.create_skill_run("poe2", "L", snap2, 1000, {}, [])  # left 'running'

    snap3 = db.insert_snapshot("poe1", "Other", 3, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    run3 = db.create_skill_run("poe1", "Other", snap3, 1000, {}, [])
    db.finish_skill_run(run3, "finished", {})

    found = db.latest_finished_skill_run("poe2")
    assert found == {"run_id": run1, "snapshot_id": snap1, "league": "L", "days_since_league_start": 5, "finished_at": found["finished_at"]}
    assert run2  # the still-running run exists but must never be returned as "finished"
    assert db.latest_finished_skill_run("poe1")["run_id"] == run3
    db.close()


def test_latest_finished_skill_run_picks_the_newest_of_several_finished_runs(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    for i in range(3):
        snap = db.insert_snapshot("poe2", "L", i, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
        run = db.create_skill_run("poe2", "L", snap, 1000, {}, [])
        db.finish_skill_run(run, "finished", {})
    assert db.latest_finished_skill_run("poe2")["days_since_league_start"] == 2
    db.close()


def test_latest_finished_skill_run_none_when_nothing_finished(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    assert db.latest_finished_skill_run("poe2") is None
    snap = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    db.create_skill_run("poe2", "L", snap, 1000, {}, [])  # still running
    assert db.latest_finished_skill_run("poe2") is None
    db.close()


def test_get_skill_run_population_total(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    snap = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    run = db.create_skill_run("poe2", "L", snap, 124408, {}, [])
    assert db.get_skill_run_population_total(run) == 124408
    assert db.get_skill_run_population_total(999999) == 0
    db.close()


def test_iter_skill_stat_passive_population_for_snapshot(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    snap = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    stat = {"level_bracket": "90-100", "slot": "ring", "stat_id": "s1", "mod_kind": "explicit", "count": 2,
            "population": 4, "slot_holders": 3, "usage_pct": 50.0, "value_min": 1.0, "value_max": 2.0}
    passive = {"level_bracket": "90-100", "keypassive": "K", "count": 1, "population": 4, "usage_pct": 25.0}
    pop = {"level_bracket": "90-100", "population": 4, "facet_count": 500}
    db.replace_skill_counts_for_snapshot(snap, "poe2", 5, "main", "Fireball", [stat], [passive], [pop])

    stat_rows = db.iter_skill_stat_counts_for_snapshot(snap)
    assert stat_rows == [("main", "Fireball", "90-100", "ring", "s1", "explicit", 2, 4, 3, 50.0, 1.0, 2.0)]
    passive_rows = db.iter_skill_passive_counts_for_snapshot(snap)
    assert passive_rows == [("main", "Fireball", "90-100", "K", 1, 4, 25.0)]
    pop_rows = db.iter_skill_populations_for_snapshot(snap)
    assert pop_rows == [("main", "Fireball", "90-100", 4, 500)]

    # a different snapshot must see none of this
    other = db.insert_snapshot("poe2", "L", 6, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    assert db.iter_skill_stat_counts_for_snapshot(other) == []
    db.close()


def test_iter_skill_stat_and_passive_counts_for_league_only_reads_skill_top100_snapshots(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    skill_snap = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    legacy_snap = db.insert_snapshot("poe2", "L", 6, 0, 0, 0)  # defaults to legacy
    stat = {"level_bracket": "90-100", "slot": "ring", "stat_id": "s1", "mod_kind": "explicit", "count": 2,
            "population": 4, "slot_holders": 3, "usage_pct": 50.0, "value_min": 1.0, "value_max": 2.0}
    passive = {"level_bracket": "90-100", "keypassive": "K", "count": 1, "population": 4, "usage_pct": 25.0}
    db.replace_skill_counts_for_snapshot(skill_snap, "poe2", 5, "main", "Fireball", [stat], [passive], [])
    # simulate a hypothetical legacy write directly (should never happen via real code paths)
    db._conn.execute(
        "INSERT INTO skill_stat_counts (snapshot_id, game, skill_type, skill, level_bracket, slot, stat_id, "
        "mod_kind, count, population, slot_holders, usage_pct, value_min, value_max, days_since_league_start, created_at) "
        "VALUES (?, 'poe2', 'main', 'Fireball', '90-100', 'ring', 's1', 'explicit', 99, 4, 3, 50.0, 1.0, 2.0, 6, '2026-01-01T00:00:00Z')",
        (legacy_snap,),
    )
    db._conn.commit()

    stat_rows = db.iter_skill_stat_counts_for_league("poe2", "L")
    assert len(stat_rows) == 1 and stat_rows[0][11] == skill_snap  # snapshot_id column
    passive_rows = db.iter_skill_passive_counts_for_league("poe2", "L")
    assert len(passive_rows) == 1 and passive_rows[0][6] == skill_snap
    db.close()


# --- Concurrency robustness (2026-09-22 "database is locked" incident) -----

def test_uses_wal_journal_mode_and_a_generous_busy_timeout(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    db.close()


def test_a_long_held_reader_does_not_block_a_concurrent_writer(tmp_path):
    """Reproduces the 2026-09-22 incident: a read-only connection holding an
    open transaction (standing in for an ad-hoc diagnostic query against the
    live production file) must not make a concurrent write raise "database is
    locked" -- the whole reason for WAL mode, not just a bigger timeout."""
    path = tmp_path / "t.sqlite3"
    db = LocalDb(path)
    db.insert_snapshot(game="poe2", league="L", days_since_league_start=1, sample_total=0, unique_characters_fetched=0, fetch_failed=0)

    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM snapshots").fetchall()  # open read transaction, never committed/closed

    try:
        db.insert_snapshot(game="poe2", league="L", days_since_league_start=2, sample_total=0, unique_characters_fetched=0, fetch_failed=0)
    finally:
        reader.close()
        db.close()


# --- Skill-centric retention (skill-meta phase 22, approved 2026-09-22) ----

def test_cleanup_finished_skill_run_checkpoints_only_touches_finished_runs(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    lists = [{"skill_type": "main", "skill": "A", "list_name": "A", "facet_count": 9}]

    def make_run(status):
        snap = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
        run_id = db.create_skill_run("poe2", "L", snap, 1000, {}, lists)
        db.save_skill_list_members(run_id, "main", "A", "A", 9, [
            {"account": "x", "name": "X", "level": 99, "class": "Slayer", "keypassives": ()},
        ])
        if status != "running":
            db.finish_skill_run(run_id, status, {})
        return run_id

    finished = make_run("finished")
    running = make_run("running")
    failed = make_run("failed")

    removed = db.cleanup_finished_skill_run_checkpoints()

    assert removed == 2  # one skill_run_lists row + one skill_run_members row, for the finished run only
    assert db.pending_skill_lists(finished) == [] and db.iter_skill_members(finished, "main", "A") == []
    assert db.skill_list_counts(running) == {"done": 1}  # untouched -- still resumable
    assert db.skill_list_counts(failed) == {"done": 1}   # untouched -- kept for debugging
    db.close()


def test_cleanup_finished_skill_run_checkpoints_is_idempotent(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    snap = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    run_id = db.create_skill_run("poe2", "L", snap, 1000, {}, [{"skill_type": "main", "skill": "A", "list_name": "A", "facet_count": 9}])
    db.finish_skill_run(run_id, "finished", {})
    assert db.cleanup_finished_skill_run_checkpoints() == 1  # the one skill_run_lists row
    assert db.cleanup_finished_skill_run_checkpoints() == 0  # nothing left to remove
    db.close()


def test_cleanup_old_skill_stat_counts_respects_the_age_cutoff_and_collection_method(tmp_path):
    db = LocalDb(tmp_path / "t.sqlite3")
    old_snap = db.insert_snapshot("poe2", "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    new_snap = db.insert_snapshot("poe2", "L", 6, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    legacy_snap = db.insert_snapshot("poe2", "L", 7, 0, 0, 0)  # legacy_slot_top100, never has these rows normally
    stat = {"level_bracket": "90-100", "slot": "ring", "stat_id": "s1", "mod_kind": "explicit", "count": 2,
            "population": 4, "slot_holders": 3, "usage_pct": 50.0, "value_min": None, "value_max": None}
    pop = {"level_bracket": "90-100", "population": 4, "facet_count": 500}
    db.replace_skill_counts_for_snapshot(old_snap, "poe2", 5, "main", "A", [stat], [], [pop])
    db.replace_skill_counts_for_snapshot(new_snap, "poe2", 6, "main", "A", [stat], [], [pop])
    db._conn.execute("UPDATE snapshots SET created_at = '2020-01-01T00:00:00Z' WHERE id = ?", (old_snap,))
    db._conn.commit()

    deleted = db.cleanup_old_skill_stat_counts(older_than_days=45)

    assert deleted == {"skill_stat_counts": 1, "skill_passive_counts": 0, "skill_populations": 1}
    assert db.iter_skill_stat_counts_for_snapshot(old_snap) == []
    assert len(db.iter_skill_stat_counts_for_snapshot(new_snap)) == 1  # recent snapshot untouched
    assert db._conn.execute("SELECT COUNT(*) FROM snapshots WHERE id = ?", (old_snap,)).fetchone()[0] == 1  # snapshot row itself kept
    db.close()
