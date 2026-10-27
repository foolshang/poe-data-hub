"""archive_stream.build_league_archive_to_file vs the old in-memory builder.

The old builder (`archive_export.build_league_archive`) is the reference: for
the same database, with no previous archive, the parsed JSON must be equal up
to key order and the order of rows that tie on `count`.
"""
from __future__ import annotations

import io
import json
import random
import sqlite3

import pytest

from src.processing.archive_export import build_league_archive
from src.processing.archive_stream import _NestedJsonWriter, build_league_archive_to_file
from src.storage.local_db import COLLECTION_METHOD_SKILL, LocalDb

GAME, LEAGUE = "poe1", "Test League"
FIXED_TS = "2026-01-01T00:00:00Z"


def _build_db(path, seed: int = 7):
    rnd = random.Random(seed)
    db = LocalDb(path)
    slots = ["ring", "body", "weapon"]
    archetypes = ["spell-fire", "attack-melee", "minion"]
    skills = ["Fireball", "Cleave", "Raise Zombie", "Arc"]
    brackets = ["80-89", "90-100"]
    stats = [f"explicit.stat_{i}" for i in range(12)]
    kinds = ["explicit", "implicit", "fractured"]
    days_choices = [None, 0, 1, 5, 21, 22, 40, 60, 61, 90]

    def vals():
        lo = rnd.choice([None, float(rnd.randint(1, 50))])
        hi = None if lo is None else lo + rnd.randint(0, 40)
        return lo, hi

    # legacy snapshots (feed mod_counts / skill_mod_counts / keypassive_counts)
    for _ in range(6):
        days = rnd.choice(days_choices)
        sid = db.insert_snapshot(game=GAME, league=LEAGUE, days_since_league_start=days, sample_total=1,
                                 unique_characters_fetched=1, fetch_failed=0)
        mod_rows, smc_rows, kp_rows = [], [], []
        for slot in slots:
            for arch in archetypes:
                for st in rnd.sample(stats, 5):
                    lo, hi = vals()
                    mod_rows.append({"slot": slot, "archetype": arch, "stat_id": st, "mod_kind": rnd.choice(kinds),
                                     "count": rnd.randint(1, 4), "usage_pct": 1.0, "rank_in_slot": 1,
                                     "value_min": lo, "value_max": hi})
        for slot in slots:
            for skill in skills:
                for br in brackets:
                    for arch in rnd.sample(archetypes, 2):
                        pop = rnd.randint(1, 30)  # one population per (snapshot, slot, skill, bracket, archetype)
                        for st in rnd.sample(stats, 4):
                            lo, hi = vals()
                            smc_rows.append({"slot": slot, "main_skill": skill, "level_bracket": br, "archetype": arch,
                                             "stat_id": st, "mod_kind": rnd.choice(kinds), "count": rnd.randint(1, 5),
                                             "population": pop, "usage_pct": 1.0, "rank_in_bracket": 1,
                                             "value_min": lo, "value_max": hi})
        for skill in skills:
            for br in brackets:
                for arch in rnd.sample(archetypes, 2):
                    pop = rnd.randint(1, 30)
                    for kp in rnd.sample(["Keystone A", "Keystone B", "Notable C"], 2):
                        kp_rows.append({"main_skill": skill, "level_bracket": br, "archetype": arch, "keypassive": kp,
                                        "count": rnd.randint(1, 5), "population": pop, "usage_pct": 1.0,
                                        "rank_in_bracket": 1})
        db.insert_mod_counts(sid, _dedupe(mod_rows, ("slot", "archetype", "stat_id", "mod_kind")))
        db.insert_skill_mod_counts(sid, GAME, days, _dedupe(
            smc_rows, ("slot", "main_skill", "level_bracket", "archetype", "stat_id", "mod_kind")))
        db.insert_keypassive_counts(sid, GAME, days, _dedupe(kp_rows, ("main_skill", "level_bracket", "archetype", "keypassive")))

    # skill-centric snapshots
    for _ in range(5):
        days = rnd.choice(days_choices)
        sid = db.insert_snapshot(game=GAME, league=LEAGUE, days_since_league_start=days, sample_total=1,
                                 unique_characters_fetched=1, fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL)
        for stype, skill in [("main", "Fireball"), ("main", "Cleave"), ("spirit", "Fireball"), ("main", "Arc")]:
            stat_rows, pass_rows, pop_rows = [], [], []
            for br in brackets:
                pop = rnd.randint(2, 40)
                pop_rows.append({"level_bracket": br, "population": pop, "facet_count": pop * 3})
                for slot in slots:
                    for st in rnd.sample(stats, 4):
                        lo, hi = vals()
                        stat_rows.append({"level_bracket": br, "slot": slot, "stat_id": st, "mod_kind": rnd.choice(kinds),
                                          "count": rnd.randint(1, 4), "population": pop, "slot_holders": 1, "usage_pct": 1.0,
                                          "value_min": lo, "value_max": hi})
                for kp in rnd.sample(["Keystone A", "Keystone B", "Notable C"], 2):
                    pass_rows.append({"level_bracket": br, "keypassive": kp, "count": rnd.randint(1, 4),
                                      "population": pop, "usage_pct": 1.0})
            if skill == "Arc":
                stat_rows = []  # passive-only leaf
            if stype == "spirit":
                pass_rows = []  # stat-only leaf
            db.replace_skill_counts_for_snapshot(
                sid, GAME, days, stype, skill, _dedupe(stat_rows, ("level_bracket", "slot", "stat_id", "mod_kind")),
                pass_rows, pop_rows)
        # a population row for a skill that has no stat or passive row at all must not create a leaf
        db.replace_skill_counts_for_snapshot(sid, GAME, days, "main", "Ghost", [], [], [{"level_bracket": "90-100", "population": 9, "facet_count": 9}])

    # noise: another game and another league
    for game, league in [("poe2", LEAGUE), (GAME, "Other League")]:
        sid = db.insert_snapshot(game=game, league=league, days_since_league_start=5, sample_total=1,
                                 unique_characters_fetched=1, fetch_failed=0)
        db.insert_mod_counts(sid, [{"slot": "ring", "archetype": "x", "stat_id": "noise", "mod_kind": "explicit",
                                    "count": 99, "usage_pct": 1.0, "rank_in_slot": 1, "value_min": None, "value_max": None}])
    db.commit()
    return db


def _dedupe(rows, key_fields):
    seen, out = set(), []
    for r in rows:
        k = tuple(r[f] for f in key_fields)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _canonical(node):
    """Order-insensitive form: dict keys sorted, every list of rows sorted by its JSON text."""
    if isinstance(node, dict):
        return {k: _canonical(v) for k, v in sorted(node.items())}
    if isinstance(node, list):
        return sorted((_canonical(v) for v in node), key=lambda v: json.dumps(v, sort_keys=True))
    return node


def _assert_count_descending(node):
    if isinstance(node, dict):
        for v in node.values():
            _assert_count_descending(v)
    elif isinstance(node, list) and node and all(isinstance(r, dict) and "count" in r for r in node):
        counts = [r["count"] for r in node]
        assert counts == sorted(counts, reverse=True)


def test_matches_old_builder_on_a_mixed_database(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    db = _build_db(db_path)
    old = build_league_archive(db, GAME, LEAGUE)
    db.close()
    out = tmp_path / "out" / "a.json"
    build_league_archive_to_file(db_path, GAME, LEAGUE, out, exported_at=FIXED_TS)
    new = json.loads(out.read_text(encoding="utf-8"))

    assert new["exported_at"] == FIXED_TS
    old["exported_at"] = FIXED_TS
    for dim in ("mod_counts", "skill_mod_counts", "passives", "skills"):
        assert old[dim], f"fixture must exercise {dim}"
        assert _canonical(new[dim]) == _canonical(old[dim]), dim
    assert _canonical(new) == _canonical(old)
    _assert_count_descending(new)
    assert "Ghost" not in json.dumps(new["skills"])


def test_equivalence_check_can_fail(tmp_path):
    """Mutation check: the comparison above must notice a single changed count."""
    db_path = tmp_path / "t.sqlite3"
    db = _build_db(db_path)
    old = build_league_archive(db, GAME, LEAGUE)
    db.close()
    out = tmp_path / "a.json"
    build_league_archive_to_file(db_path, GAME, LEAGUE, out, exported_at=old["exported_at"])
    new = json.loads(out.read_text(encoding="utf-8"))
    first_leaf = next(iter(new["mod_counts"]["ring"].values()))
    first_leaf[next(iter(first_leaf))][0]["count"] += 1
    assert _canonical(new) != _canonical(old)


def test_empty_league_gives_empty_dimensions(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    LocalDb(db_path).close()
    out = tmp_path / "a.json"
    build_league_archive_to_file(db_path, GAME, "Nothing", out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mod_counts"] == data["skill_mod_counts"] == data["passives"] == data["skills"] == {}


def test_never_writes_the_database(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    _build_db(db_path).close()
    before = db_path.read_bytes()
    build_league_archive_to_file(db_path, GAME, LEAGUE, tmp_path / "a.json")
    assert db_path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        from src.processing.archive_stream import open_readonly
        open_readonly(db_path).execute("DELETE FROM snapshots")


def test_a_failed_build_leaves_no_file_and_keeps_the_previous_one(tmp_path):
    db_path = tmp_path / "t.sqlite3"
    _build_db(db_path).close()
    out = tmp_path / "a.json"
    out.write_text("previous", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE skill_populations")  # makes the skills dimension raise mid-write
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.OperationalError):
        build_league_archive_to_file(db_path, GAME, LEAGUE, out)
    assert out.read_text(encoding="utf-8") == "previous"
    assert not (tmp_path / "a.json.tmp").exists()


@pytest.mark.parametrize("paths", [
    [],
    [("a", "b", "c")],
    [("a", "b", "c"), ("a", "b", "d"), ("a", "e", "f"), ("g", "h", "i"), ("g", "j", "k"), ("l", "m", "n")],
    [("x", "1"), ("x", "2"), ("y", "3")],
])
def test_nested_writer_produces_valid_json_matching_the_paths(paths):
    buf = io.StringIO()
    w = _NestedJsonWriter(buf)
    w.begin()
    expected: dict = {}
    for i, p in enumerate(paths):
        w.emit(p, [i])
        node = expected
        for k in p[:-1]:
            node = node.setdefault(k, {})
        node[p[-1]] = [i]
    w.end()
    assert json.loads(buf.getvalue()) == expected
