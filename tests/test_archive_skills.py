"""Accumulating skills archive: per-snapshot merge, multi-source, state-file rules, and the files other apps read."""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.processing.archive_export import build_league_archive
from src.processing.archive_runner import export_league_archive_streaming
from src.processing.archive_skills import (
    ArchiveStateError, LegacyArchiveError, SnapshotConflictError, SourceNotIndexedError, open_state,
    restore_state_from_skills_file, render_skills_file,
)
from src.processing.archive_stream import build_league_archive_to_file
from src.storage.local_db import COLLECTION_METHOD_SKILL, LocalDb
from src.storage.publisher import PricesPublisher
from tests.test_archive_stream import GAME, LEAGUE, _build_db, _canonical

NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _skill_snapshot_ids(path: Path) -> list[int]:
    c = sqlite3.connect(path)
    try:
        return [r[0] for r in c.execute(
            "SELECT id FROM snapshots WHERE collection_method = ? AND league = ? ORDER BY id", (COLLECTION_METHOD_SKILL, LEAGUE))]
    finally:
        c.close()


def _set_runs(path: Path, statuses: dict[int, str], started_at: str | None = None) -> None:
    """One skill_runs row per snapshot with the given status (replaces any existing)."""
    c = sqlite3.connect(path)
    try:
        c.execute("DELETE FROM skill_runs")
        for sid, status in statuses.items():
            c.execute(
                "INSERT INTO skill_runs (game, league, snapshot_id, status, started_at, plan_json) VALUES (?,?,?,?,?,'{}')",
                (GAME, LEAGUE, sid, status, started_at or _iso(NOW)))
        c.commit()
    finally:
        c.close()


def _db(tmp_path: Path, name: str = "live.sqlite3", seed: int = 7) -> Path:
    path = tmp_path / name
    _build_db(path, seed).close()
    _set_runs(path, {sid: "finished" for sid in _skill_snapshot_ids(path)})
    return path


def _age_out(path: Path, snapshot_ids: list[int]) -> None:
    """What retention does: the raw rows of those snapshots disappear, the snapshot and its run stay."""
    c = sqlite3.connect(path)
    try:
        for sid in snapshot_ids:
            for table in ("skill_stat_counts", "skill_passive_counts", "skill_populations"):
                c.execute(f"DELETE FROM {table} WHERE snapshot_id = ?", (sid,))
        c.commit()
    finally:
        c.close()


def _copy(src: Path, dst: Path) -> Path:
    shutil.copyfile(src, dst)
    return dst


def _export(tmp_path, db, **kw):
    return export_league_archive_streaming(db, GAME, LEAGUE, archive_dir=tmp_path / "archive", **kw)


def _skills(result):
    return json.loads(result.skills_path.read_text(encoding="utf-8"))


def _reference_skills(tmp_path: Path, db: Path) -> dict:
    out = tmp_path / "reference.json"
    build_league_archive_to_file(db, GAME, LEAGUE, out)
    return json.loads(out.read_text(encoding="utf-8"))["skills"]


def test_accumulated_skills_equal_the_one_shot_build_when_nothing_has_aged_out(tmp_path):
    db = _db(tmp_path)
    result = _export(tmp_path, db)
    data = _skills(result)
    assert data["skills"], "fixture must produce skill leaves"
    assert _canonical(data["skills"]) == _canonical(_reference_skills(tmp_path, db))
    assert data["merged_snapshot_ids"] == _skill_snapshot_ids(db)
    assert result.merge.merged == _skill_snapshot_ids(db)


def test_second_run_adds_nothing_and_a_new_snapshot_is_added_exactly_once(tmp_path):
    db = _db(tmp_path)
    first = _export(tmp_path, db)
    before = _canonical(_skills(first)["skills"])
    again = _export(tmp_path, db)
    assert again.merge.merged == [] and again.merge.already_merged == len(_skill_snapshot_ids(db))
    assert _canonical(_skills(again)["skills"]) == before

    # one more finished snapshot appears (a fresh DB with the same seed reuses ids, so add via LocalDb)
    ldb = LocalDb(db)
    sid = ldb.insert_snapshot(game=GAME, league=LEAGUE, days_since_league_start=3, sample_total=1,
                              unique_characters_fetched=1, fetch_failed=0, collection_method=COLLECTION_METHOD_SKILL)
    ldb.replace_skill_counts_for_snapshot(
        sid, GAME, 3, "main", "Fireball",
        [{"level_bracket": "90-100", "slot": "ring", "stat_id": "explicit.stat_0", "mod_kind": "explicit", "count": 4,
          "population": 10, "slot_holders": 1, "usage_pct": 40.0, "value_min": 1.0, "value_max": 9.0}],
        [], [{"level_bracket": "90-100", "population": 10, "facet_count": 10}])
    ldb.close()
    _set_runs(db, {s: "finished" for s in _skill_snapshot_ids(db)})
    third = _export(tmp_path, db)
    assert third.merge.merged == [sid]
    assert _canonical(_skills(third)["skills"]) == _canonical(_reference_skills(tmp_path, db))


def test_snapshot_present_in_both_sources_is_counted_once(tmp_path):
    live = _db(tmp_path)
    copy = _copy(live, tmp_path / "copy.sqlite3")
    result = _export(tmp_path, live, extra_sources=[copy])
    assert result.merge.merged == _skill_snapshot_ids(live)
    assert set(result.merge.merged_from.values()) == {"live"}
    assert _canonical(_skills(result)["skills"]) == _canonical(_reference_skills(tmp_path, live))


def test_snapshot_missing_from_live_is_taken_from_the_copy(tmp_path):
    live = _db(tmp_path)
    copy = _copy(live, tmp_path / "copy.sqlite3")
    expected = _canonical(_reference_skills(tmp_path, live))
    aged = _skill_snapshot_ids(live)[:3]
    _age_out(live, aged)  # retention ran on the live DB after the copy was taken

    result = _export(tmp_path, live, extra_sources=[copy])
    assert {sid: result.merge.merged_from[sid] for sid in aged} == {sid: "copy1" for sid in aged}
    assert _canonical(_skills(result)["skills"]) == expected
    assert result.merge.unavailable == []

    # and without the copy those snapshots would have been lost: reported, not silently dropped
    lost = export_league_archive_streaming(live, GAME, LEAGUE, archive_dir=tmp_path / "archive2")
    assert sorted(lost.merge.unavailable) == aged


def test_a_snapshot_already_merged_survives_retention_on_every_source(tmp_path):
    live = _db(tmp_path)
    first = _export(tmp_path, live)
    expected = _canonical(_skills(first)["skills"])
    _age_out(live, _skill_snapshot_ids(live))  # everything aged out afterwards
    later = _export(tmp_path, live)
    assert later.merge.merged == [] and later.merge.unavailable == []
    assert _canonical(_skills(later)["skills"]) == expected


def test_same_snapshot_id_with_different_content_raises_and_changes_nothing(tmp_path):
    live = _db(tmp_path)
    other = _db(tmp_path, "other.sqlite3", seed=99)  # same ids, different content
    with pytest.raises(SnapshotConflictError):
        _export(tmp_path, live, extra_sources=[other])
    state = tmp_path / "archive" / "poe1-test-league.skills.state.sqlite3"
    conn = sqlite3.connect(state)
    try:
        assert conn.execute("SELECT COUNT(*) FROM merged_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stat_acc").fetchone()[0] == 0
    finally:
        conn.close()


def test_conflict_is_detected_even_when_only_the_row_counts_differ(tmp_path):
    live = _db(tmp_path)
    copy = _copy(live, tmp_path / "copy.sqlite3")
    c = sqlite3.connect(copy)
    c.execute("DELETE FROM skill_stat_counts WHERE id = (SELECT MAX(id) FROM skill_stat_counts)")
    c.commit()
    c.close()
    with pytest.raises(SnapshotConflictError):
        _export(tmp_path, live, extra_sources=[copy])


def test_only_finished_snapshots_are_merged_and_stale_running_is_reported_in_memory_only(tmp_path):
    live = _db(tmp_path)
    ids = _skill_snapshot_ids(live)
    old = _iso(NOW - timedelta(hours=100))
    c = sqlite3.connect(live)
    c.execute("DELETE FROM skill_runs")
    rows = [(ids[0], "finished", _iso(NOW)), (ids[1], "failed", _iso(NOW)), (ids[2], "running", old),
            (ids[3], "running", _iso(NOW)), (ids[4], "abandoned", old)]
    for sid, status, started in rows:
        c.execute("INSERT INTO skill_runs (game, league, snapshot_id, status, started_at, plan_json) VALUES (?,?,?,?,?,'{}')",
                  (GAME, LEAGUE, sid, status, started))
    c.commit()
    c.close()
    before = live.read_bytes()

    result = _export(tmp_path, live, resume_max_age_hours=48)
    m = result.merge
    assert m.merged == [ids[0]]
    assert m.stale_running == [ids[2]] and m.active_running == [ids[3]]
    assert sorted(m.not_finished) == sorted([ids[1], ids[4]])
    assert live.read_bytes() == before, "the database must not be written (stale running stays 'running' in it)"
    assert _skills(result)["merged_snapshot_ids"] == [ids[0]]


def test_a_copy_that_caught_a_run_half_way_does_not_supply_its_rows(tmp_path):
    live = _db(tmp_path)
    copy = _copy(live, tmp_path / "copy.sqlite3")
    sid = _skill_snapshot_ids(live)[-1]
    _set_runs(copy, {s: ("running" if s == sid else "finished") for s in _skill_snapshot_ids(copy)})  # copy: still running
    _age_out(live, [sid])  # live: finished, rows aged out
    result = _export(tmp_path, live, extra_sources=[copy])
    assert result.merge.unavailable == [sid]
    assert sid not in _skills(result)["merged_snapshot_ids"]


def test_a_source_without_snapshot_indexes_is_refused(tmp_path):
    live = _db(tmp_path)
    copy = _copy(live, tmp_path / "copy.sqlite3")
    c = sqlite3.connect(copy)
    c.execute("DROP INDEX idx_skill_stat_counts_snapshot")
    c.commit()
    c.close()
    with pytest.raises(SourceNotIndexedError):
        _export(tmp_path, live, extra_sources=[copy])


def test_sources_are_opened_read_only(tmp_path):
    live = _db(tmp_path)
    copy = _copy(live, tmp_path / "copy.sqlite3")
    before = (live.read_bytes(), copy.read_bytes())
    _export(tmp_path, live, extra_sources=[copy])
    assert (live.read_bytes(), copy.read_bytes()) == before


def test_a_crash_in_the_middle_of_a_snapshot_merges_nothing_of_it(tmp_path, monkeypatch):
    live = _db(tmp_path)
    from src.processing import archive_skills as mod

    real = mod._stream
    calls = {"n": 0}

    def flaky(conn, sql, params, shape):
        it = real(conn, sql, params, shape)
        if "skill_passive_counts" in sql:
            calls["n"] += 1
            if calls["n"] == 3:  # third snapshot, after its stat rows were already upserted
                raise RuntimeError("boom")
        return it

    monkeypatch.setattr(mod, "_stream", flaky)
    with pytest.raises(RuntimeError):
        _export(tmp_path, live)
    monkeypatch.undo()

    state = tmp_path / "archive" / "poe1-test-league.skills.state.sqlite3"
    conn = sqlite3.connect(state)
    done = [r[0] for r in conn.execute("SELECT snapshot_id FROM merged_snapshots ORDER BY 1")]
    conn.close()
    assert len(done) == 2
    resumed = _export(tmp_path, live)  # the next run merges the rest, counting nothing twice
    assert _canonical(_skills(resumed)["skills"]) == _canonical(_reference_skills(tmp_path, live))


def test_a_skills_file_without_a_state_file_refuses_to_merge(tmp_path):
    live = _db(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "poe1-test-league.skills.json").write_text('{"skills":{}}', encoding="utf-8")
    with pytest.raises(LegacyArchiveError):
        _export(tmp_path, live)


def test_an_unreadable_state_file_raises_and_is_not_recreated(tmp_path):
    live = _db(tmp_path)
    _export(tmp_path, live)
    state = tmp_path / "archive" / "poe1-test-league.skills.state.sqlite3"
    state.write_bytes(b"this is not a database" * 100)
    with pytest.raises(ArchiveStateError):
        _export(tmp_path, live)
    assert state.read_bytes().startswith(b"this is not a database")


def test_a_state_file_of_another_league_is_refused(tmp_path):
    state = tmp_path / "s.sqlite3"
    open_state(state, "poe1", "A", create=True).close()
    with pytest.raises(ArchiveStateError):
        open_state(state, "poe1", "B", create=False)


def test_rebuild_keeps_every_old_file_as_legacy_and_starts_from_the_sources(tmp_path):
    live = _db(tmp_path)
    first = _export(tmp_path, live)
    original = {p: p.read_bytes() for p in (first.base_path, first.skills_path)}
    result = _export(tmp_path, live, rebuild=True)
    moved = {p.name for p in result.moved_to_legacy}
    assert moved == {first.base_path.name + ".legacy", first.skills_path.name + ".legacy",
                     first.state_path.name + ".legacy", first.state_path.name + ".bak.legacy"}
    for p, data in original.items():
        assert p.with_name(p.name + ".legacy").read_bytes() == data
    assert result.merge.merged == _skill_snapshot_ids(live)

    again = _export(tmp_path, live, rebuild=True)  # a second rebuild must not overwrite the first .legacy
    assert any(".legacy." in p.name for p in again.moved_to_legacy)
    assert first.base_path.with_name(first.base_path.name + ".legacy").read_bytes() == original[first.base_path]


def test_the_first_accumulating_export_keeps_an_older_base_archive_as_legacy(tmp_path):
    live = _db(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "poe1-test-league.json").write_text('{"old":"format"}', encoding="utf-8")
    result = _export(tmp_path, live)
    assert (archive / "poe1-test-league.json.legacy").read_text(encoding="utf-8") == '{"old":"format"}'
    assert result.base_path.read_text(encoding="utf-8") != '{"old":"format"}'
    _export(tmp_path, live)  # later runs overwrite the (derived) base file without producing more .legacy files
    assert not list(archive.glob("*.legacy.*"))


def test_state_backup_is_refreshed_before_a_merge_that_changes_it(tmp_path):
    live = _db(tmp_path)
    result = _export(tmp_path, live)
    bak = result.state_path.with_name(result.state_path.name + ".bak")
    assert bak.exists()
    conn = sqlite3.connect(bak)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM merged_snapshots").fetchone()[0] == 0  # the state as it was before this merge
    conn.close()


def test_state_can_be_rebuilt_from_the_rendered_json(tmp_path):
    live = _db(tmp_path)
    result = _export(tmp_path, live)
    restored = tmp_path / "restored.sqlite3"
    counts = restore_state_from_skills_file(result.skills_path, restored)
    assert counts["leaves"] > 0
    conn = open_state(restored, GAME, LEAGUE, create=False)
    out = tmp_path / "rerender.json"
    render_skills_file(conn, GAME, LEAGUE, out, exported_at="2026-01-01T00:00:00Z")
    conn.close()
    original = _skills(result)
    again = json.loads(out.read_text(encoding="utf-8"))
    for key in ("skills", "merged_snapshot_ids"):
        assert again[key] == original[key]


def _state_tables(path: Path) -> dict:
    conn = open_state(path, GAME, LEAGUE, create=False)
    try:
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("stat_acc", "passive_acc", "pop_acc")}
    finally:
        conn.close()


def test_restored_state_plus_export_against_the_same_database_counts_nothing_twice(tmp_path):
    live = _db(tmp_path)
    first = _export(tmp_path, live)
    ids = _skill_snapshot_ids(live)
    assert _skills(first)["merged_snapshot_ids"] == ids, "the rendered file must carry every merged id"
    # the disaster: the state file is gone, only the rendered skills file survived
    new_dir = tmp_path / "archive2"
    new_dir.mkdir()
    restored_state = new_dir / first.state_path.name
    restore_state_from_skills_file(first.skills_path, restored_state)
    again = export_league_archive_streaming(live, GAME, LEAGUE, archive_dir=new_dir)
    assert again.merge.merged == [] and again.merge.already_merged == len(ids)
    ta, tb = _state_tables(restored_state), _state_tables(first.state_path)
    for t in ta:
        assert sorted(ta[t]) == sorted(tb[t]), (t, set(ta[t]) ^ set(tb[t]))
    a, b = _skills(first), _skills(again)
    a["exported_at"] = b["exported_at"] = ""
    assert a == b
    assert again.skills_bytes == first.skills_bytes


def test_base_archive_keeps_the_shape_the_older_builder_wrote(tmp_path):
    live = _db(tmp_path)
    result = _export(tmp_path, live)
    base = json.loads(result.base_path.read_text(encoding="utf-8"))
    ldb = LocalDb(live)
    old = build_league_archive(ldb, GAME, LEAGUE)
    ldb.close()
    assert set(base) == set(old) - {"skills"}
    assert base["schema_version"] == old["schema_version"] == 1
    for k in ("game", "league", "league_slug", "age_buckets"):
        assert base[k] == old[k]
    for dim in ("mod_counts", "skill_mod_counts", "passives"):
        assert _canonical(base[dim]) == _canonical(old[dim])


# ---- publisher.publish_file -----------------------------------------------------------

class _Blob:
    store: dict = {}

    def __init__(self, name, corrupt=False):
        self.name, self.cache_control, self.content_encoding, self.corrupt = name, None, None, corrupt
        self.md5_hash, self.size = None, None

    def upload_from_filename(self, filename, content_type=None):
        import base64, hashlib
        data = Path(filename).read_bytes()
        self.size = len(data) + (1 if self.corrupt else 0)
        self.md5_hash = base64.b64encode(hashlib.md5(data).digest()).decode()
        _Blob.store[self.name] = (data, self.content_encoding, content_type, self.cache_control)

    def reload(self):
        pass


class _Bucket:
    def __init__(self, corrupt=False):
        self.corrupt = corrupt

    def blob(self, name):
        return _Blob(name, self.corrupt)


class _Client:
    def __init__(self, corrupt=False):
        self.corrupt = corrupt

    def bucket(self, name):
        return _Bucket(self.corrupt)


def test_publish_file_uploads_a_gzip_and_verifies_md5_and_size(tmp_path):
    src = tmp_path / "a.json"
    src.write_text(json.dumps({"x": list(range(1000))}), encoding="utf-8")
    pub = PricesPublisher(bucket_name="b", local_cache_dir=tmp_path / "c")
    pub._client = _Client()
    pub.publish_file(src, "archive/a.json", "public, max-age=3600", gzip_upload=True)
    data, enc, ctype, cc = _Blob.store["archive/a.json"]
    assert enc == "gzip" and ctype == "application/json" and cc == "public, max-age=3600"
    assert gzip.decompress(data) == src.read_bytes()
    assert not (tmp_path / "a.json.upload.gz").exists()

    pub.publish_file(src, "archive/plain.json", "public", gzip_upload=False)
    assert _Blob.store["archive/plain.json"][0] == src.read_bytes()


def test_publish_file_raises_on_a_size_mismatch_and_still_cleans_up(tmp_path):
    src = tmp_path / "a.json"
    src.write_text("{}", encoding="utf-8")
    pub = PricesPublisher(bucket_name="b", local_cache_dir=tmp_path / "c")
    pub._client = _Client(corrupt=True)
    with pytest.raises(RuntimeError, match="MISMATCH"):
        pub.publish_file(src, "archive/a.json", "public", gzip_upload=True)
    assert not (tmp_path / "a.json.upload.gz").exists()


def test_runner_reports_publish_failures_without_raising(tmp_path):
    live = _db(tmp_path)

    class Boom:
        def publish_file(self, *a, **k):
            raise RuntimeError("offline")

    result = _export(tmp_path, live, publisher=Boom())
    assert len(result.publish_errors) == 2 and result.base_path.exists() and result.skills_path.exists()


# ---- tools/prepare_archive_copy ----------------------------------------------------------

def test_prepare_archive_copy_extracts_and_indexes_without_touching_the_gz(tmp_path):
    from tools.prepare_archive_copy import prepare

    live = _db(tmp_path)
    raw = tmp_path / "plain.sqlite3"
    shutil.copyfile(live, raw)
    c = sqlite3.connect(raw)
    for name in ("idx_skill_stat_counts_snapshot", "idx_skill_passive_counts_snapshot", "idx_skill_populations_snapshot"):
        c.execute(f"DROP INDEX IF EXISTS {name}")
    c.commit()
    c.close()
    gz = tmp_path / "copy.sqlite3.gz"
    with open(raw, "rb") as fin, gzip.open(gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    gz_before = gz.read_bytes()
    out = tmp_path / "extracted.sqlite3"
    prepare(gz, out)
    assert gz.read_bytes() == gz_before
    with pytest.raises(SystemExit):
        prepare(gz, out)  # never overwrites
    result = _export(tmp_path, live, extra_sources=[out])  # accepted as a source: indexes are there
    assert result.merge.merged
