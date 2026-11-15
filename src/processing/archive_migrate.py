"""Re-sort an archive state accumulated under the OLD age-bucket rule into the current one (`AGE_MAPPING_VERSION`).

Why this exists: the state's cells (`stat_acc` / `passive_acc` / `pop_acc`) are sums per age bucket and do not remember which
snapshot a number came from, and `value_min` / `value_max` cannot be subtracted. Moving a snapshot out of a bucket therefore means
recomputing that bucket from the snapshots that stay; moving one into a bucket can simply be added to it.

What it does (one transaction, all or nothing, and a copy of the state is kept first):
  1. `merged_snapshots` knows every snapshot's `days`, so the old bucket (old rule) and the new bucket (current rule) of each are
     known. Snapshots whose bucket differs are "moved".
  2. A bucket that loses a snapshot is recomputed from scratch: its rows are deleted and every snapshot that belongs to it under the
     NEW rule is merged again from the sources (insurance copies / live database). A bucket that only gains snapshots just gets them added.
  3. Before anything is changed it checks that every snapshot it has to read is in a source with exactly the row counts and
     `created_at` recorded in the state (otherwise it stops and says which); after, that the grand totals of `count` /
     `population` are unchanged (snapshots only moved between buckets).
  4. `meta.age_mapping` is set to the current version. A state of the current version is refused ("already migrated").

It is the same result as a full rebuild (tests build both on the same fixture and compare every row), but it reads only the
snapshots of the buckets that change, not the whole league.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from src.processing.archive_export import AGE_MAPPING_VERSION, UNKNOWN_BUCKET, _age_bucket, _now_iso
from src.processing.archive_skills import (
    ArchiveStateError, Source, _SnapState, _accumulate_snapshot, _fill_counts, _scan_source,
)
from src.processing.archive_stream import open_readonly

log = logging.getLogger(__name__)

OLD_MAPPING_VERSION = 1
_ACC_TABLES = ("stat_acc", "passive_acc", "pop_acc")


def old_age_bucket(days: Optional[int]) -> str:
    """The rule used before 2026-11 (mapping version 1): 0-based days compared as if 1-based; days < 1 -> unknown."""
    if days is None or days < 1:
        return UNKNOWN_BUCKET
    if days <= 21:
        return "1-21"
    if days <= 60:
        return "22-60"
    return "61+"


@dataclass
class MigrationReport:
    game: str
    league: str
    moved: dict = field(default_factory=dict)            # snapshot_id -> (old bucket, new bucket)
    recomputed_buckets: list = field(default_factory=list)
    added_only_buckets: list = field(default_factory=list)
    snapshots_read: dict = field(default_factory=dict)   # snapshot_id -> source name
    totals_before: dict = field(default_factory=dict)
    totals_after: dict = field(default_factory=dict)
    applied: bool = False
    backup: Optional[Path] = None


def _totals(state: sqlite3.Connection) -> dict:
    return {
        "stat_count": state.execute("SELECT COALESCE(SUM(count), 0) FROM stat_acc").fetchone()[0],
        "passive_count": state.execute("SELECT COALESCE(SUM(count), 0) FROM passive_acc").fetchone()[0],
        "population": state.execute("SELECT COALESCE(SUM(population), 0) FROM pop_acc").fetchone()[0],
    }


def migrate_state(
    state_path: Path, sources: Sequence[Source], game: str, league: str, apply: bool = False,
) -> MigrationReport:
    """Plan (apply=False) or perform (apply=True) the re-sort of `state_path`. Reads `sources` `mode=ro`. Raises
    `ArchiveStateError` without changing anything when the state cannot be migrated safely."""
    state_path = Path(state_path)
    if not state_path.exists():
        raise ArchiveStateError(f"state file {state_path} does not exist")
    state = sqlite3.connect(state_path, timeout=30, isolation_level=None)
    report = MigrationReport(game, league)
    conns: dict[str, sqlite3.Connection] = {}
    try:
        meta = dict(state.execute("SELECT key, value FROM meta").fetchall())
        if (meta.get("game"), meta.get("league")) != (game, league):
            raise ArchiveStateError(f"state file {state_path} belongs to {meta.get('game')}/{meta.get('league')}")
        if meta.get("schema_version") != "1":
            raise ArchiveStateError(f"state file {state_path} has schema_version {meta.get('schema_version')!r}")
        version = int(meta.get("age_mapping") or OLD_MAPPING_VERSION)
        if version == AGE_MAPPING_VERSION:
            raise ArchiveStateError(f"state file {state_path} is already migrated (age_mapping {version})")
        if version != OLD_MAPPING_VERSION:
            raise ArchiveStateError(f"state file {state_path} has age_mapping {version}; only {OLD_MAPPING_VERSION} can be migrated")

        rows = state.execute(
            "SELECT snapshot_id, source, created_at, days, stat_rows, passive_rows, pop_rows FROM merged_snapshots ORDER BY snapshot_id"
        ).fetchall()
        restored = [r[0] for r in rows if r[1] == "restored"]
        if restored:
            raise ArchiveStateError(
                f"snapshots {restored[:10]} were restored from a skills file and have no recorded days: their old bucket "
                "cannot be told, so this state cannot be migrated (rebuild it instead)")
        recorded = {r[0]: r for r in rows}
        new_bucket = {sid: _age_bucket(r[3]) for sid, r in recorded.items()}
        old_bucket = {sid: old_age_bucket(r[3]) for sid, r in recorded.items()}
        report.moved = {sid: (old_bucket[sid], new_bucket[sid]) for sid in recorded if old_bucket[sid] != new_bucket[sid]}
        leaving = {o for o, _n in report.moved.values()}
        joining = {n for _o, n in report.moved.values()}
        report.recomputed_buckets = sorted(leaving)
        report.added_only_buckets = sorted(joining - leaving)
        to_read = sorted(
            [sid for sid in recorded if new_bucket[sid] in leaving]
            + [sid for sid, (_o, n) in report.moved.items() if n in report.added_only_buckets])

        # find, for every snapshot that has to be read again, a source that holds exactly what the state recorded
        cutoff = "9999-12-31T00:00:00Z"   # nothing is "stale running": only finished snapshots matter here
        scans: dict[str, dict[int, _SnapState]] = {}
        for src in sources:
            conn = open_readonly(src.path)
            conns[src.name] = conn
            scans[src.name] = _scan_source(conn, game, league, cutoff)
        chosen: dict[int, tuple[str, _SnapState]] = {}
        missing: list[str] = []
        for sid in to_read:
            _sid, _src_name, created_at, days, stat_rows, passive_rows, pop_rows = recorded[sid]
            found = None
            for src in sources:
                st = scans[src.name].get(sid)
                if st is None or st.status != "finished":
                    continue
                _fill_counts(conns[src.name], st)
                if (st.created_at, st.days, st.stat_rows, st.passive_rows, st.pop_rows) == (
                        created_at, days, stat_rows, passive_rows, pop_rows):
                    found = (src.name, st)
                    break
            if found is None:
                missing.append(f"{sid} (days {days}, rows {stat_rows}/{passive_rows}/{pop_rows})")
            else:
                chosen[sid] = found
        if missing:
            raise ArchiveStateError(
                "no source holds these snapshots with the row counts the state recorded, so the buckets "
                f"{report.recomputed_buckets + report.added_only_buckets} cannot be rebuilt; nothing was changed: {missing}")
        report.snapshots_read = {sid: name for sid, (name, _st) in chosen.items()}
        report.totals_before = _totals(state)
        if not apply:
            return report

        report.backup = state_path.with_name(state_path.name + ".pre-age-mapping-1")
        if report.backup.exists():
            raise ArchiveStateError(f"{report.backup} already exists; refusing to overwrite it")
        state.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(state_path, report.backup)

        state.execute("BEGIN IMMEDIATE")
        try:
            for bucket in report.recomputed_buckets:
                for table in _ACC_TABLES:
                    state.execute(f"DELETE FROM {table} WHERE bucket = ?", (bucket,))
            for sid in to_read:
                name, st = chosen[sid]
                _accumulate_snapshot(state, conns[name], st, name, new_bucket[sid])
            report.totals_after = _totals(state)
            if report.totals_after != report.totals_before:
                raise ArchiveStateError(
                    f"totals changed by the migration (before {report.totals_before}, after {report.totals_after}); rolled back")
            state.execute("INSERT OR REPLACE INTO meta VALUES ('age_mapping', ?)", (str(AGE_MAPPING_VERSION),))
            state.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_age_mapping_at', ?)", (_now_iso(),))
            state.execute("COMMIT")
        except BaseException:
            state.execute("ROLLBACK")
            raise
        report.applied = True
        log.info("archive migrate %s/%s: moved %s, recomputed %s, added to %s", game, league, report.moved,
                 report.recomputed_buckets, report.added_only_buckets)
        return report
    finally:
        for c in conns.values():
            c.close()
        state.close()
