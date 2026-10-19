"""The streamed skill publish must produce EXACTLY the files the old fetchall-everything version did.

`_old_publish` below is the pre-2026-10-19 `publish_skill_meta` body (kept verbatim as an oracle); the new one is
compared with it byte for byte (same JSON serialization the real publisher uses) on randomized data that includes
the awkward cases: ties in `count`, rows under min_usage_pct, a skill with a population but no stat rows, a skill
whose rows are all filtered out, stat rows of a skill that has no population row, and (fallback) rows of one skill
that are not contiguous."""
import json
import random
from unittest.mock import MagicMock, patch

import pytest

from src.main import publish_skill_meta
from src.processing.skill_publish import (
    NonContiguousRows,
    attach_skill_tags,
    build_skill_dictionary,
    build_skill_groups,
    build_skill_passives_schema,
    build_skills_index,
    iter_skill_mods,
)
from src.storage.local_db import COLLECTION_METHOD_SKILL, LocalDb

FROZEN = "2026-10-19T00:00:00+00:00"


class CapturePublisher:
    """Stores the exact bytes PricesPublisher.publish would upload, per path."""

    def __init__(self):
        self.files: dict[str, tuple[str, str, bool]] = {}
        self.order: list[str] = []

    def publish(self, data, blob_path, cache_control, compact=False):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(data, ensure_ascii=False, indent=2)
        self.files[blob_path] = (payload, cache_control, compact)
        self.order.append(blob_path)


def _old_publish(db, publisher, game, cfg, normalizer, gems):
    """pre-2026-10-19 publish_skill_meta, verbatim apart from the gems argument and the return value."""
    run = db.latest_finished_skill_run(game)
    publish_cfg = cfg.get("skill_meta", {}).get("publish", {})
    min_usage_pct = publish_cfg.get("min_usage_pct", 0.1)
    index_max_age = publish_cfg.get("index_cache_max_age_seconds", 3600)
    skill_max_age = publish_cfg.get("skill_file_cache_max_age_seconds", 21600)
    dict_max_age = publish_cfg.get("dictionary_cache_max_age_seconds", 86400)
    snapshot_id, league, days = run["snapshot_id"], run["league"], run["days_since_league_start"]
    stat_rows = db.iter_skill_stat_counts_for_snapshot(snapshot_id)
    passive_rows = db.iter_skill_passive_counts_for_snapshot(snapshot_id)
    population_rows = db.iter_skill_populations_for_snapshot(snapshot_id)
    groups = build_skill_groups_oracle(stat_rows, passive_rows, population_rows, min_usage_pct)
    population_total = db.get_skill_run_population_total(run["run_id"])
    groups, _ = attach_skill_tags(groups, gems)
    publisher.publish(build_skills_index(game, league, days, population_total, groups), f"{game}/skills/index.json",
                      cache_control=f"public, max-age={index_max_age}")
    for g in groups:
        publisher.publish(g.to_file(game, league, days), g.path(game), cache_control=f"public, max-age={skill_max_age}", compact=True)
    publisher.publish(build_skill_dictionary(game, normalizer.stat_dictionary()), f"{game}/skills/dictionary.json",
                      cache_control=f"public, max-age={dict_max_age}")
    publisher.publish(build_skill_passives_schema(game, league, days, groups), f"{game}/passives/latest.json",
                      cache_control="public, max-age=3600")


def build_skill_groups_oracle(stat_rows, passive_rows, population_rows, min_usage_pct):
    """The pre-2026-10-19 build_skill_groups, verbatim (one pass over ALL rows)."""
    from src.processing.skill_publish import SkillGroup, _unique_slugs
    facet_count, populations = {}, {}
    for skill_type, skill, bracket, population, facet in population_rows:
        key = (skill_type, skill)
        facet_count[key] = facet
        populations.setdefault(key, {})[bracket] = population
    mods = {}
    for skill_type, skill, bracket, slot, stat_id, mod_kind, count, population, slot_holders, usage_pct, v_min, v_max in stat_rows:
        if usage_pct < min_usage_pct:
            continue
        mods.setdefault((skill_type, skill), {}).setdefault(bracket, {}).setdefault(slot, []).append({
            "stat_id": stat_id, "mod_kind": mod_kind, "count": count, "population": population,
            "slot_holders": slot_holders, "usage_pct": usage_pct, "value_min": v_min, "value_max": v_max,
        })
    passives = {}
    for skill_type, skill, bracket, keypassive, count, population, usage_pct in passive_rows:
        if usage_pct < min_usage_pct:
            continue
        passives.setdefault((skill_type, skill), {}).setdefault(bracket, []).append({
            "keypassive": keypassive, "count": count, "population": population, "usage_pct": usage_pct,
        })
    for by_bracket in mods.values():
        for by_slot in by_bracket.values():
            for rows in by_slot.values():
                rows.sort(key=lambda r: -r["count"])
    for by_bracket in passives.values():
        for rows in by_bracket.values():
            rows.sort(key=lambda r: -r["count"])
    types = {skill_type for skill_type, _skill in populations}
    slugs = {t: _unique_slugs(sorted((s for (ty, s) in populations if ty == t), key=lambda s: -facet_count[(t, s)])) for t in types}
    groups = [
        SkillGroup(skill_type=st, skill=sk, slug=slugs[st][sk], facet_count=facet_count.get((st, sk), 0),
                   populations=populations[(st, sk)], mods=mods.get((st, sk), {}), passives=passives.get((st, sk), {}))
        for (st, sk) in populations
    ]
    groups.sort(key=lambda g: (g.skill_type, -g.facet_count, g.skill))
    return groups


def _stat(bracket, slot, stat_id, count, usage):
    return {"level_bracket": bracket, "slot": slot, "stat_id": stat_id, "mod_kind": "explicit", "count": count,
            "population": 100, "slot_holders": 90, "usage_pct": usage, "value_min": 1.0, "value_max": 2.0}


def _seed(db, rng, game="poe2", skills=40):
    snap = db.insert_snapshot(game, "L", 5, 0, 0, 0, collection_method=COLLECTION_METHOD_SKILL)
    gems = []
    for i in range(skills):
        skill_type = "spirit" if i % 5 == 0 else "main"
        name = f"Skill {i}" if i % 7 else f"Skill {i} (Alt)"
        pops = [{"level_bracket": b, "population": rng.randint(1, 500), "facet_count": rng.choice([10, 500, 500, 1200])}
                for b in ("80-89", "90-100")]
        facet = pops[0]["facet_count"]
        for p in pops:
            p["facet_count"] = facet
        stats = []
        if i % 11 != 3:                                           # skill 3,14,25,.. has a population but NO stat rows
            for b in ("80-89", "90-100"):
                for slot in ("ring", "amulet", "body"):
                    for k in range(rng.randint(0, 12)):
                        usage = 0.05 if (i % 11 == 6) else rng.choice([0.05, 0.1, 0.5, 20.0, 80.0])   # skill 6,17..: ALL rows filtered
                        stats.append(_stat(b, slot, f"st{rng.randint(0, 30)}", rng.choice([5, 5, 5, 9, 40]), usage))   # many ties
        passives = [{"level_bracket": "90-100", "keypassive": f"kp{k}", "count": rng.choice([3, 3, 8]), "population": 10,
                     "usage_pct": rng.choice([0.05, 4.0, 30.0])} for k in range(rng.randint(0, 5))]
        db.replace_skill_counts_for_snapshot(snap, game, 5, skill_type, name, stats, passives, pops)
        gems.append({"display_name": name, "is_support": False, "tags": ["fire"] if i % 2 else ["cold", "aoe"]})
    # stat rows of a skill that has no population row at all (never published before, must not be now)
    db._conn.execute(
        "INSERT INTO skill_stat_counts (snapshot_id, game, skill_type, skill, level_bracket, slot, stat_id, mod_kind, count, population,"
        " slot_holders, usage_pct, value_min, value_max, days_since_league_start, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (snap, game, "main", "Ghost Skill", "90-100", "ring", "stX", "explicit", 7, 10, 9, 50.0, 1.0, 2.0, 5, "t"))
    db._conn.commit()
    run_id = db.create_skill_run(game, "L", snap, 4242, {}, [])
    db.finish_skill_run(run_id, "finished", {})
    return gems


def _compare(tmp_path, seed, break_contiguity=False):
    db = LocalDb(tmp_path / f"t{seed}.sqlite3")
    gems = _seed(db, random.Random(seed))
    if break_contiguity:
        # re-insert one row of an early skill AFTER the others -> its rows are no longer contiguous
        db._conn.execute(
            "INSERT INTO skill_stat_counts (snapshot_id, game, skill_type, skill, level_bracket, slot, stat_id, mod_kind, count, population,"
            " slot_holders, usage_pct, value_min, value_max, days_since_league_start, created_at) "
            "SELECT snapshot_id, game, skill_type, skill, level_bracket, slot, 'stLATE', mod_kind, 99, population, slot_holders, usage_pct,"
            " value_min, value_max, days_since_league_start, created_at FROM skill_stat_counts WHERE skill='Skill 1' LIMIT 1")
        db._conn.commit()
    normalizer = MagicMock(stat_dictionary=lambda: [{"stat_id": "st1", "template": "+# x", "mod_kind": "explicit"}])
    cfg = {"firebase": {"bucket": "b"}, "skill_meta": {"publish": {"min_usage_pct": 0.1}}}
    http = MagicMock()
    http.get_json.return_value = {"gems": gems}

    old, new = CapturePublisher(), CapturePublisher()
    with patch("src.processing.skill_publish._now_iso", return_value=FROZEN):
        _old_publish(db, old, "poe2", cfg, normalizer, gems)
        publish_skill_meta(db, new, "poe2", cfg, normalizer, http)
    db.close()
    return old, new


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_streamed_publish_is_byte_identical_to_the_old_buffered_publish(tmp_path, seed):
    old, new = _compare(tmp_path, seed)
    assert sorted(new.files) == sorted(old.files)                       # same set of files (incl. no Ghost Skill)
    assert len(old.files) > 40
    for path in old.files:
        assert new.files[path] == old.files[path], path                 # payload bytes, cache-control and compact flag
    assert new.order[0] == old.order[0] == "poe2/skills/index.json"     # index still goes first
    assert not any("ghost" in p for p in new.files)


def test_fallback_when_a_skills_rows_are_not_contiguous_is_still_identical(tmp_path):
    old, new = _compare(tmp_path, 7, break_contiguity=True)
    assert sorted(new.files) == sorted(old.files)
    for path in old.files:
        assert new.files[path] == old.files[path], path


def test_iter_skill_mods_yields_one_skill_at_a_time_and_rejects_interleaving():
    row = lambda s, st, c, u=50.0: ("main", s, "90-100", "ring", st, "explicit", c, 10, 9, u, 1.0, 2.0)
    got = dict(iter_skill_mods([row("A", "x", 1), row("A", "y", 9), row("B", "z", 3, u=0.01)], 0.1))
    assert [r["stat_id"] for r in got[("main", "A")]["90-100"]["ring"]] == ["y", "x"]       # biggest count first
    assert got[("main", "B")] == {}                                                        # all filtered -> empty, still yielded
    with pytest.raises(NonContiguousRows):
        list(iter_skill_mods([row("A", "x", 1), row("B", "z", 3), row("A", "y", 9)], 0.1))
