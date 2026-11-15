"""`archive_migrate.migrate_state`: a state accumulated under the old age-bucket rule, re-sorted, equals a full rebuild.

The proof is a comparison with the real thing: the same database is exported once under the OLD rule (patched in, state stamped
`age_mapping` 1 as a shipped state would be), migrated, and compared row for row with an export under the current rule.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.processing import archive_skills
from src.processing.archive_export import AGE_MAPPING_VERSION
from src.processing.archive_migrate import migrate_state, old_age_bucket
from src.processing.archive_runner import export_league_archive_streaming
from src.processing.archive_skills import ArchiveStateError, Source
from tests.test_archive_skills import GAME, LEAGUE, _age_out, _db, _skill_snapshot_ids

ACC = ("stat_acc", "passive_acc", "pop_acc")


def _set_days(db: Path, days_by_position: list) -> list[int]:
    ids = _skill_snapshot_ids(db)
    assert len(ids) == len(days_by_position)
    c = sqlite3.connect(db)
    try:
        for sid, d in zip(ids, days_by_position):
            c.execute("UPDATE snapshots SET days_since_league_start = ? WHERE id = ?", (d, sid))
            for table in ("skill_stat_counts", "skill_passive_counts"):
                c.execute(f"UPDATE {table} SET days_since_league_start = ? WHERE snapshot_id = ?", (d, sid))
        c.commit()
    finally:
        c.close()
    return ids


def _old_state(tmp_path: Path, db: Path, monkeypatch, name: str = "old") -> Path:
    """What the VM holds today: a state merged under the old rule, no/old age_mapping stamp."""
    monkeypatch.setattr(archive_skills, "_age_bucket", old_age_bucket)
    result = export_league_archive_streaming(db, GAME, LEAGUE, archive_dir=tmp_path / name)
    monkeypatch.undo()
    c = sqlite3.connect(result.state_path)
    c.execute("DELETE FROM meta WHERE key = 'age_mapping'")      # shipped states carry no stamp at all
    c.commit()
    c.close()
    return result.state_path


def _rows(state_path: Path) -> dict:
    c = sqlite3.connect(state_path)
    try:
        out = {t: sorted(c.execute(f"SELECT * FROM {t}").fetchall(), key=repr) for t in ACC}
        out["merged"] = c.execute("SELECT snapshot_id, source, created_at, days, stat_rows, passive_rows, pop_rows "
                                  "FROM merged_snapshots ORDER BY 1").fetchall()
        out["age_mapping"] = dict(c.execute("SELECT key, value FROM meta").fetchall()).get("age_mapping")
        return out
    finally:
        c.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# first set moves 0 (unknown->1-21), 21 (1-21->22-60), 60 (22-60->61+); 20 and 61 stay. second has an untouched unknown
DAYS_SETS = [[0, 20, 21, 60, 61], [None, 21, 22, 59, 60], [None, 0, 1, 5, 40], [20, 21, 21, 60, 60]]


@pytest.mark.parametrize("days", DAYS_SETS, ids=lambda d: "-".join("N" if x is None else str(x) for x in d))
def test_migrated_state_equals_a_full_rebuild_under_the_current_rule(tmp_path, monkeypatch, days):
    db = _db(tmp_path)
    _set_days(db, days)
    old = _old_state(tmp_path, db, monkeypatch)
    full = export_league_archive_streaming(db, GAME, LEAGUE, archive_dir=tmp_path / "full").state_path
    assert _rows(old) != _rows(full) or all(old_age_bucket(d) == archive_skills._age_bucket(d) for d in days)

    report = migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)

    assert report.applied and report.backup is not None and report.backup.exists()
    got, want = _rows(old), _rows(full)
    assert got["age_mapping"] == str(AGE_MAPPING_VERSION)
    assert got == want                                            # every cell, count, value_min/max, population, snapshot record
    assert report.totals_before == report.totals_after


def test_the_state_is_usable_afterwards_and_a_second_run_is_refused(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _set_days(db, [0, 20, 21, 60, 61])
    old = _old_state(tmp_path, db, monkeypatch)
    migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)
    with pytest.raises(ArchiveStateError, match="already migrated"):
        migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)
    # an export on top of the migrated state merges nothing new and renders the current layout
    result = export_league_archive_streaming(db, GAME, LEAGUE, archive_dir=old.parent)
    assert result.merge.merged == [] and result.skills_bytes > 0


def test_dry_run_changes_nothing_and_says_what_it_would_do(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _set_days(db, [0, 20, 21, 60, 61])
    old = _old_state(tmp_path, db, monkeypatch)
    before = _sha(old)
    report = migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=False)
    assert _sha(old) == before and not report.applied
    assert sorted(report.moved.values()) == [("1-21", "22-60"), ("22-60", "61+"), ("unknown", "1-21")]
    assert report.recomputed_buckets == ["1-21", "22-60", "unknown"] and report.added_only_buckets == ["61+"]


def test_a_snapshot_of_a_changing_bucket_that_no_source_holds_stops_everything(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ids = _set_days(db, [0, 20, 21, 60, 61])
    old = _old_state(tmp_path, db, monkeypatch)
    _age_out(db, [ids[1]])                      # days 20 stays in 1-21, which is recomputed -> its rows are needed
    before = _sha(old)
    with pytest.raises(ArchiveStateError, match=str(ids[1])):
        migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)
    assert _sha(old) == before                  # nothing written, no backup left behind
    assert not list(old.parent.glob("*.pre-age-mapping-1"))


def test_snapshots_in_buckets_that_do_not_change_may_be_aged_out(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ids = _set_days(db, [None, 21, 22, 59, 60])
    old = _old_state(tmp_path, db, monkeypatch)
    full_db = tmp_path / "full.sqlite3"
    shutil.copyfile(db, full_db)                # the same database, before retention: what a full rebuild would read
    _age_out(db, [ids[0]])                      # days NULL: unknown stays unknown, never touched
    full = export_league_archive_streaming(full_db, GAME, LEAGUE, archive_dir=tmp_path / "full").state_path
    migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)
    assert _rows(old) == _rows(full)


def test_a_source_whose_row_counts_differ_from_the_record_is_not_trusted(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ids = _set_days(db, [0, 20, 21, 60, 61])
    old = _old_state(tmp_path, db, monkeypatch)
    c = sqlite3.connect(db)
    c.execute("DELETE FROM skill_stat_counts WHERE rowid IN (SELECT rowid FROM skill_stat_counts WHERE snapshot_id = ? LIMIT 1)", (ids[2],))
    c.commit()
    c.close()
    with pytest.raises(ArchiveStateError, match=str(ids[2])):
        migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)


def test_a_state_with_restored_snapshots_is_refused(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _set_days(db, [0, 20, 21, 60, 61])
    old = _old_state(tmp_path, db, monkeypatch)
    c = sqlite3.connect(old)
    c.execute("UPDATE merged_snapshots SET source = 'restored', days = NULL WHERE rowid = (SELECT MIN(rowid) FROM merged_snapshots)")
    c.commit()
    c.close()
    with pytest.raises(ArchiveStateError, match="restored"):
        migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)


def test_wrong_league_is_refused(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _set_days(db, [0, 20, 21, 60, 61])
    old = _old_state(tmp_path, db, monkeypatch)
    with pytest.raises(ArchiveStateError, match="belongs to"):
        migrate_state(old, [Source("live", db)], GAME, "Another League", apply=True)


def test_old_rule_is_the_one_that_was_in_force():
    """Pinned so a later edit cannot rewrite history: mapping version 1 = days<1 unknown, <=21, <=60, else 61+."""
    assert [old_age_bucket(d) for d in (None, -1, 0, 1, 21, 22, 60, 61)] == [
        "unknown", "unknown", "unknown", "1-21", "1-21", "22-60", "22-60", "61+"]


def test_a_migration_that_loses_rows_is_rolled_back_by_the_totals_check(tmp_path, monkeypatch):
    from src.processing import archive_migrate
    db = _db(tmp_path)
    _set_days(db, [0, 20, 21, 60, 61])
    old = _old_state(tmp_path, db, monkeypatch)
    before = _rows(old)
    calls = []
    real = archive_migrate._accumulate_snapshot

    def lossy(state, src, st, name, bucket):
        calls.append(st.snapshot_id)
        if len(calls) > 1:                      # the first snapshot is added, every later one silently dropped
            return
        real(state, src, st, name, bucket)

    monkeypatch.setattr(archive_migrate, "_accumulate_snapshot", lossy)
    with pytest.raises(ArchiveStateError, match="totals changed"):
        migrate_state(old, [Source("live", db)], GAME, LEAGUE, apply=True)
    assert _rows(old) == before                 # rolled back: the state is exactly as it was (meta stamp included)
