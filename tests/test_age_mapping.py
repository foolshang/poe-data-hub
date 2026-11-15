"""The archive's age buckets: one definition, `unknown` only for an unknown league start, a negative `days` is loud.

`days_since_league_start` = `(job time - start_at).days`, 0-based (day 0 = the first 24 h). The names "1-21 / 22-60 / 61+"
count league days from 1, so the bounds in days are 0..20 / 21..59 / 60+ (SPEC "Age-bucket mapping"). Before 2026-11 the
code compared the 0-based value against 1/21/22/60/61: every edge one day late and day 0 in "unknown".
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from src import main as hub_main
from src.processing import archive_stream
from src.processing.archive_export import (
    AGE_BUCKET_BOUNDS, AGE_BUCKET_LABELS, AGE_MAPPING_VERSION, UNKNOWN_BUCKET, _age_bucket, age_bucket_sql,
)
from src.processing.archive_runner import export_league_archive_streaming
from src.processing.archive_skills import (
    RENDER_VERSION, ArchiveStateError, open_state, restore_state_from_skills_file, skills_file_is_current,
)
from src.storage.local_db import COLLECTION_METHOD_SKILL, LocalDb

GAME, LEAGUE = "poe1", "Edge League"

# days -> bucket, the whole contract in one table
EXPECTED = {
    None: "unknown",
    -30: "1-21", -1: "1-21",          # a negative value is a wrong start_at: first bucket, reported by the callers
    0: "1-21", 1: "1-21", 19: "1-21", 20: "1-21",
    21: "22-60", 22: "22-60", 58: "22-60", 59: "22-60",
    60: "61+", 61: "61+", 62: "61+", 400: "61+",
}


@pytest.mark.parametrize("days,bucket", list(EXPECTED.items()), ids=[f"days={d}" for d in EXPECTED])
def test_python_mapping_every_edge(days, bucket):
    assert _age_bucket(days) == bucket


def test_unknown_means_a_missing_start_only():
    assert [d for d in range(-100, 1000) if _age_bucket(d) == UNKNOWN_BUCKET] == []
    assert _age_bucket(None) == UNKNOWN_BUCKET


def test_names_count_league_days_from_one():
    """"1-21" is league days 1..21 = days 0..20 (day 0 is the first day), 22-60 is days 21..59, 61+ starts at day 60."""
    first_day = {label: lo for label, lo, _hi in AGE_BUCKET_BOUNDS}
    assert first_day == {"1-21": 0, "22-60": 21, "61+": 60}
    for label, lo, hi in AGE_BUCKET_BOUNDS:
        name_lo = int(label.split("-")[0].rstrip("+"))
        assert lo + 1 == name_lo, label                      # name counts from 1, days from 0
        if hi is not None:
            assert hi + 1 == int(label.split("-")[1]), label


def test_sql_form_agrees_with_python_form_for_every_value():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (d INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(d,) for d in [None, *range(-40, 200)]])
    rows = conn.execute(f"SELECT d, {age_bucket_sql('d')} FROM t").fetchall()
    assert len(rows) == 241
    for d, got in rows:
        assert got == _age_bucket(d), d
    # and the helper the streaming SQL really uses is that same generator
    assert archive_stream._bucket("x") == age_bucket_sql("x")


def test_labels_and_version_are_published_in_one_place():
    assert AGE_BUCKET_LABELS == ["1-21", "22-60", "61+", "unknown"]
    assert AGE_MAPPING_VERSION == 2 and RENDER_VERSION >= 3


# ---- the real export paths (legacy tables + skills state) over a database with every edge ------------------------------

def _edge_db(path: Path, days_list) -> dict:
    """One legacy snapshot and one finished skill-centric snapshot per days value; every row carries a stat/skill name that
    encodes its days so the output says which bucket each ended in. Returns {days: skill_snapshot_id}."""
    db = LocalDb(path)
    skill_ids = {}
    for d in days_list:
        tag = "none" if d is None else str(d)
        sid = db.insert_snapshot(game=GAME, league=LEAGUE, days_since_league_start=d, sample_total=1,
                                 unique_characters_fetched=1, fetch_failed=0)
        db.insert_mod_counts(sid, [{"slot": "ring", "archetype": "a", "stat_id": f"leg_{tag}", "mod_kind": "explicit",
                                    "count": 1, "usage_pct": 1.0, "rank_in_slot": 1, "value_min": None, "value_max": None}])
        db.insert_skill_mod_counts(sid, GAME, d, [{
            "slot": "ring", "main_skill": "Fireball", "level_bracket": "90-100", "archetype": "a", "stat_id": f"smc_{tag}",
            "mod_kind": "explicit", "count": 1, "population": 1, "usage_pct": 1.0, "rank_in_bracket": 1,
            "value_min": None, "value_max": None}])
        ssid = db.insert_snapshot(game=GAME, league=LEAGUE, days_since_league_start=d, sample_total=1,
                                  unique_characters_fetched=1, fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL)
        db.replace_skill_counts_for_snapshot(
            ssid, GAME, d, "main", "Fireball",
            [{"level_bracket": "90-100", "slot": "ring", "stat_id": f"skl_{tag}", "mod_kind": "explicit", "count": 1,
              "population": 1, "slot_holders": 1, "usage_pct": 1.0, "value_min": None, "value_max": None}],
            [{"level_bracket": "90-100", "keypassive": f"kp_{tag}", "count": 1, "population": 1, "usage_pct": 1.0}],
            [{"level_bracket": "90-100", "population": 1, "facet_count": 1}])
        skill_ids[d] = ssid
    db.commit()
    db.close()
    conn = sqlite3.connect(path)
    try:
        for ssid in skill_ids.values():
            conn.execute("INSERT INTO skill_runs (game, league, snapshot_id, status, started_at, plan_json) "
                         "VALUES (?,?,?,?,?,'{}')", (GAME, LEAGUE, ssid, "finished", "2026-11-01T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()
    return skill_ids


def _bucket_of_names(node, prefix: str, found=None, bucket=None):
    """Walk a nested archive dict; map the tagged name (leg_*, smc_*, skl_*, kp_*) to the bucket key above it."""
    found = {} if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            _bucket_of_names(v, prefix, found, k if k in AGE_BUCKET_LABELS else bucket)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                for key in ("stat_id", "keypassive"):
                    name = item.get(key)
                    if isinstance(name, str) and name.startswith(prefix):
                        found[name[len(prefix):]] = bucket
            _bucket_of_names(item, prefix, found, bucket)
    return found


EDGE_DAYS = [None, -1, 0, 1, 20, 21, 59, 60, 61]


@pytest.fixture
def exported(tmp_path):
    db = tmp_path / "live.sqlite3"
    ids = _edge_db(db, EDGE_DAYS)
    result = export_league_archive_streaming(db, GAME, LEAGUE, archive_dir=tmp_path / "archive")
    return (result, ids, json.loads(result.base_path.read_text(encoding="utf-8")),
            json.loads(result.skills_path.read_text(encoding="utf-8")))


def _expected_by_tag():
    return {("none" if d is None else str(d)): EXPECTED[d] for d in EDGE_DAYS}


def test_streaming_sql_puts_every_legacy_and_skill_row_in_the_right_bucket(exported):
    _result, _ids, base, skills = exported
    want = _expected_by_tag()
    assert _bucket_of_names(base["mod_counts"], "leg_") == want
    assert _bucket_of_names(base["skill_mod_counts"], "smc_") == want
    assert _bucket_of_names(skills["skills"], "skl_") == want
    assert _bucket_of_names(skills["skills"], "kp_") == want


def test_headers_carry_the_mapping_version(exported):
    _result, _ids, base, skills = exported
    for doc in (base, skills):
        assert doc["age_mapping"] == AGE_MAPPING_VERSION
        assert doc["age_buckets"] == AGE_BUCKET_LABELS
    assert skills["render_version"] == RENDER_VERSION


def test_state_is_stamped_and_refused_under_another_mapping(exported, tmp_path):
    result, _ids, _base, _skills = exported
    state_path = result.state_path
    conn = open_state(state_path, GAME, LEAGUE, create=False)           # same version: fine
    assert dict(conn.execute("SELECT key, value FROM meta").fetchall())["age_mapping"] == str(AGE_MAPPING_VERSION)
    conn.close()
    for tamper in ("DELETE FROM meta WHERE key = 'age_mapping'", "UPDATE meta SET value = '1' WHERE key = 'age_mapping'"):
        c = sqlite3.connect(state_path)
        c.execute(tamper)
        c.commit()
        c.close()
        with pytest.raises(ArchiveStateError, match="age_mapping"):
            open_state(state_path, GAME, LEAGUE, create=False)
        with pytest.raises(ArchiveStateError, match="age_mapping"):          # and a whole export refuses, merges nothing
            export_league_archive_streaming(tmp_path / "live.sqlite3", GAME, LEAGUE, archive_dir=tmp_path / "archive")
        c = sqlite3.connect(state_path)
        c.execute("INSERT OR REPLACE INTO meta VALUES ('age_mapping', ?)", (str(AGE_MAPPING_VERSION),))
        c.commit()
        c.close()


def test_a_skills_file_of_another_mapping_is_rendered_again_and_cannot_seed_a_state(exported, tmp_path):
    result, _ids, _base, _skills = exported
    state = open_state(result.state_path, GAME, LEAGUE, create=False)
    try:
        assert skills_file_is_current(result.skills_path, state)
        raw = result.skills_path.read_bytes()
        marker = f'"age_mapping":{AGE_MAPPING_VERSION}'.encode()
        assert marker in raw
        old = tmp_path / "old.skills.json"
        old.write_bytes(raw.replace(marker, b'"age_mapping":1', 1))
        assert not skills_file_is_current(old, state)
        nomap = tmp_path / "nomap.skills.json"
        nomap.write_bytes(raw.replace(marker + b",", b"", 1))
        assert not skills_file_is_current(nomap, state)
    finally:
        state.close()
    for bad in (old, nomap):
        with pytest.raises(ArchiveStateError, match="age_mapping"):
            restore_state_from_skills_file(bad, tmp_path / f"{bad.stem}.state.sqlite3")
    restored = tmp_path / "ok.state.sqlite3"
    restore_state_from_skills_file(result.skills_path, restored)           # the current rendering restores
    open_state(restored, GAME, LEAGUE, create=False).close()


# ---- negative days: counted in the first bucket, never silent -------------------------------------------------------------

def test_negative_days_are_reported_with_ids_and_league(tmp_path, caplog):
    db = tmp_path / "live.sqlite3"
    ids = _edge_db(db, [-1, 0, 5])
    with caplog.at_level(logging.WARNING):
        result = export_league_archive_streaming(db, GAME, LEAGUE, archive_dir=tmp_path / "archive")
    c = sqlite3.connect(db)
    neg_ids = sorted(r[0] for r in c.execute("SELECT id FROM snapshots WHERE days_since_league_start < 0"))
    c.close()
    assert len(neg_ids) == 2 and ids[-1] in neg_ids                          # the legacy and the skill-centric snapshot
    assert result.negative_days_snapshots == neg_ids
    assert result.merge.negative_days == [ids[-1]]                           # the merge names the one it merged
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert LEAGUE in text and "NEGATIVE" in text and all(str(i) in text for i in neg_ids)
    assert f"snapshot {ids[-1]}" in text and "start_at is wrong" in text
    skills = json.loads(result.skills_path.read_text(encoding="utf-8"))
    assert _bucket_of_names(skills["skills"], "skl_")["-1"] == "1-21"       # counted in the first bucket, not dropped, not unknown


def test_no_negative_days_no_warning(tmp_path, caplog):
    db = tmp_path / "live.sqlite3"
    _edge_db(db, [None, 0, 5])
    with caplog.at_level(logging.WARNING):
        result = export_league_archive_streaming(db, GAME, LEAGUE, archive_dir=tmp_path / "archive")
    assert result.negative_days_snapshots == [] and result.merge.negative_days == []
    assert not [r for r in caplog.records if "NEGATIVE" in r.getMessage()]


class _FakePublisher:
    def __init__(self, status=None):
        self.status = status or {"errors": []}
        self.published = None

    def fetch_previous(self, _path):
        return json.loads(json.dumps(self.status))

    def publish(self, payload, _path, cache_control=None):
        self.published = payload

    def publish_file(self, *a, **k):
        pass


def test_status_json_counts_negative_days_snapshots(tmp_path, monkeypatch):
    db_path = tmp_path / "live.sqlite3"
    _edge_db(db_path, [-1, 0, 5])
    monkeypatch.setattr(hub_main.job_lock, "acquire", lambda _name: contextlib.nullcontext())
    db = LocalDb(db_path)
    pub = _FakePublisher()
    outcome = hub_main._export_league_to_archive({}, pub, db, GAME, LEAGUE, tmp_path / "archive", "weekly")
    db.close()
    assert outcome["status"] == "ok"
    assert outcome["negative_days_count"] == 2 and len(outcome["negative_days_snapshots"]) == 2
    hub_main._publish_archive_status(pub, {}, export_outcome=(GAME, outcome))
    assert pub.published["archive"]["negative_days_snapshots"] == 2
    # the other game's count is added, and a later clean export of the first game resets only its own part
    pub2 = _FakePublisher(pub.published)
    hub_main._publish_archive_status(pub2, {}, export_outcome=("poe2", {"league": "X", "at": "t", "status": "ok", "negative_days_count": 3}))
    assert pub2.published["archive"]["negative_days_snapshots"] == 5
    pub3 = _FakePublisher(pub2.published)
    hub_main._publish_archive_status(pub3, {}, export_outcome=(GAME, {**outcome, "negative_days_count": 0, "negative_days_snapshots": []}))
    assert pub3.published["archive"]["negative_days_snapshots"] == 3
