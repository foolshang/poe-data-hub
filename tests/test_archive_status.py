"""archive_status: which snapshots are protected by the archive or an insurance copy, and when the others are deleted."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.processing.archive_runner import export_league_archive_streaming
from src.processing.archive_status import compute_archive_coverage, retention_delete_at
from tests.test_archive_skills import _age_out, _copy, _db, _skill_snapshot_ids
from tests.test_archive_stream import GAME, LEAGUE
from tools.prepare_archive_copy import INDEXES


def _index(path: Path) -> Path:
    c = sqlite3.connect(path)
    for name, table in INDEXES:
        c.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}(snapshot_id)")
    c.commit()
    c.close()
    return path


def _league(cov):
    return next(e for e in cov["leagues"] if e["league"] == LEAGUE)


def test_retention_delete_at_is_the_first_cleanup_after_created_plus_45_days():
    assert retention_delete_at("2026-10-27T22:00:00Z") == datetime(2026, 12, 12, 21, 0, tzinfo=timezone.utc)
    assert retention_delete_at("2026-10-27T20:59:59Z") == datetime(2026, 12, 11, 21, 0, tzinfo=timezone.utc)
    assert retention_delete_at("2026-10-27T21:00:00Z") == datetime(2026, 12, 12, 21, 0, tzinfo=timezone.utc)


def test_without_archive_or_copy_every_snapshot_with_rows_is_at_risk(tmp_path):
    live = _db(tmp_path)
    ids = _skill_snapshot_ids(live)
    cov = compute_archive_coverage(live, tmp_path / "archive")
    e = _league(cov)
    assert e["at_risk_count"] == len(ids) and e["archive_merged"] == 0 and e["backup_covers_through"] is None
    assert cov["earliest_loss_at"] == e["earliest_loss_at"] and cov["earliest_loss_at"] is not None


def test_merged_snapshots_are_no_longer_at_risk(tmp_path):
    live = _db(tmp_path)
    export_league_archive_streaming(live, GAME, LEAGUE, archive_dir=tmp_path / "archive")
    e = _league(compute_archive_coverage(live, tmp_path / "archive"))
    assert e["at_risk_count"] == 0 and e["earliest_loss_at"] is None and e["archive_merged"] == len(_skill_snapshot_ids(live))


def test_a_copy_protects_what_it_holds_and_reports_how_far_it_reaches(tmp_path):
    live = _db(tmp_path)
    ids = _skill_snapshot_ids(live)
    copy = _index(_copy(live, tmp_path / "copy.sqlite3"))
    # snapshots taken after the copy do not exist in it
    c = sqlite3.connect(copy)
    for t in ("skill_stat_counts", "skill_passive_counts", "skill_populations"):
        c.execute(f"DELETE FROM {t} WHERE snapshot_id = ?", (ids[-1],))
    c.commit()
    c.close()
    cov = compute_archive_coverage(live, tmp_path / "archive", copies=[copy])
    e = _league(cov)
    assert [r["snapshot_id"] for r in e["at_risk"]] == [ids[-1]] and e["at_risk_count"] == 1
    assert e["backup_covers_through"]["snapshot_id"] == ids[-2] and e["backup_covers_through"]["copy"] == "copy.sqlite3"
    assert cov["copies"] == [{"name": "copy.sqlite3", "status": "ok"}]


def test_rows_already_deleted_and_in_no_source_are_lost_not_at_risk(tmp_path):
    live = _db(tmp_path)
    ids = _skill_snapshot_ids(live)
    _age_out(live, ids[:1])
    e = _league(compute_archive_coverage(live, tmp_path / "archive"))
    assert e["lost_snapshot_ids"] == ids[:1] and e["lost_count"] == 1 and e["at_risk_count"] == len(ids) - 1


def test_unindexed_missing_and_broken_copies_are_reported_and_protect_nothing(tmp_path):
    live = _db(tmp_path)
    unindexed = _copy(live, tmp_path / "plain.sqlite3")
    c = sqlite3.connect(unindexed)
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND tbl_name LIKE 'skill_%'").fetchall():
        c.execute(f"DROP INDEX {name}")
    c.commit()
    c.close()
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database" * 100)
    cov = compute_archive_coverage(live, tmp_path / "archive", copies=[unindexed, tmp_path / "gone.sqlite3", broken])
    assert [c["status"] for c in cov["copies"]][:2] == ["not_indexed", "missing"]
    assert cov["copies"][2]["status"] in ("error", "not_indexed")
    assert _league(cov)["at_risk_count"] == len(_skill_snapshot_ids(live))


def test_an_unreadable_state_file_is_reported_and_protects_nothing(tmp_path):
    live = _db(tmp_path)
    export_league_archive_streaming(live, GAME, LEAGUE, archive_dir=tmp_path / "archive")
    state = next((tmp_path / "archive").glob("*.state.sqlite3"))
    state.write_bytes(b"garbage" * 1000)
    e = _league(compute_archive_coverage(live, tmp_path / "archive"))
    assert e["archive_merged"] is None and "archive_state_error" in e and e["at_risk_count"] > 0


def test_never_writes_any_database(tmp_path):
    live = _db(tmp_path)
    copy = _index(_copy(live, tmp_path / "copy.sqlite3"))
    before = (live.read_bytes(), copy.read_bytes())
    compute_archive_coverage(live, tmp_path / "archive", copies=[copy])
    assert (live.read_bytes(), copy.read_bytes()) == before
