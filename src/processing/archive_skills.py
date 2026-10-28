"""Accumulating `skills` archive (archive rewrite, step 2 of 2).

The skill-centric tables (`skill_stat_counts`, `skill_passive_counts`,
`skill_populations`) are deleted by retention 45 days after a snapshot is taken
(`LocalDb.cleanup_old_skill_stat_counts`), so a league's `skills` archive can
no longer be recomputed from the database: it has to be *accumulated*, one
snapshot at a time, before the snapshot's rows age out.

Where the accumulation lives: a SQLite state file next to the archive
(`{game}-{slug}.skills.state.sqlite3`), not a JSON that is re-read on every run
(PoE2's rendered JSON is 125MB; parsed it is hundreds of MB, far over the VM's
460MB cap). Per snapshot, ONE state transaction adds that snapshot's rows to the
running totals AND records its id in `merged_snapshots`, so a crash can never
count a snapshot twice or half. The JSON that consumers read
(`{game}-{slug}.skills.json`) is a rendering of the state; it carries the same
`merged_snapshot_ids` list.

Rules (agreed with the project owner):
- only snapshots whose `skill_runs` row says `finished` are merged; a `running`
  row older than `resume_max_age_hours` is *treated as* abandoned (in memory,
  the database is never written) and reported;
- several sources are read in order -- the live database first, then
  `--extra-source` copies (insurance copies taken before retention ran). A
  snapshot is merged from the first source that has it finished and with rows;
  if a second source also has it, the row counts and `created_at` must agree
  or the run raises (`SnapshotConflictError`);
- every source is opened `mode=ro`; a copy must have `snapshot_id` indexes
  (`tools/prepare_archive_copy.py` creates them on an extracted copy);
- a state file that exists but cannot be read raises, it is never recreated
  silently; a skills JSON without a state file (the pre-ledger format) raises
  and asks for a rebuild.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional, Sequence

from src.processing.archive_export import SCHEMA_VERSION, _age_bucket, _now_iso
from src.processing.archive_stream import _NestedJsonWriter, assemble_skill_leaves, open_readonly
from src.storage.local_db import COLLECTION_METHOD_SKILL
from src.storage.publisher import sanitize_league_filename

log = logging.getLogger(__name__)

STATE_SCHEMA_VERSION = 1
_BATCH = 5000

_STATE_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE merged_snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    source TEXT, created_at TEXT, days INTEGER,
    stat_rows INTEGER, passive_rows INTEGER, pop_rows INTEGER, merged_at TEXT
);
CREATE TABLE stat_acc (
    skill_type TEXT NOT NULL, skill TEXT NOT NULL, level_bracket TEXT NOT NULL, bucket TEXT NOT NULL,
    slot TEXT NOT NULL, stat_id TEXT NOT NULL, mod_kind TEXT NOT NULL,
    count INTEGER NOT NULL, value_min, value_max,
    PRIMARY KEY (skill_type, skill, level_bracket, bucket, slot, stat_id, mod_kind)
) WITHOUT ROWID;
CREATE TABLE passive_acc (
    skill_type TEXT NOT NULL, skill TEXT NOT NULL, level_bracket TEXT NOT NULL, bucket TEXT NOT NULL,
    keypassive TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (skill_type, skill, level_bracket, bucket, keypassive)
) WITHOUT ROWID;
CREATE TABLE pop_acc (
    skill_type TEXT NOT NULL, skill TEXT NOT NULL, level_bracket TEXT NOT NULL, bucket TEXT NOT NULL,
    population INTEGER NOT NULL,
    PRIMARY KEY (skill_type, skill, level_bracket, bucket)
) WITHOUT ROWID;
"""

_UPSERT_STAT = """
INSERT INTO stat_acc (skill_type, skill, level_bracket, bucket, slot, stat_id, mod_kind, count, value_min, value_max)
VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT (skill_type, skill, level_bracket, bucket, slot, stat_id, mod_kind) DO UPDATE SET
    count = count + excluded.count,
    value_min = CASE WHEN excluded.value_min IS NULL THEN value_min WHEN value_min IS NULL THEN excluded.value_min
                     WHEN excluded.value_min < value_min THEN excluded.value_min ELSE value_min END,
    value_max = CASE WHEN excluded.value_max IS NULL THEN value_max WHEN value_max IS NULL THEN excluded.value_max
                     WHEN excluded.value_max > value_max THEN excluded.value_max ELSE value_max END"""
_UPSERT_PASSIVE = """
INSERT INTO passive_acc (skill_type, skill, level_bracket, bucket, keypassive, count) VALUES (?,?,?,?,?,?)
ON CONFLICT (skill_type, skill, level_bracket, bucket, keypassive) DO UPDATE SET count = count + excluded.count"""
_UPSERT_POP = """
INSERT INTO pop_acc (skill_type, skill, level_bracket, bucket, population) VALUES (?,?,?,?,?)
ON CONFLICT (skill_type, skill, level_bracket, bucket) DO UPDATE SET population = population + excluded.population"""


class ArchiveStateError(RuntimeError):
    """The state file (or the files around it) is not in a shape a merge may build on."""


class LegacyArchiveError(ArchiveStateError):
    """A skills archive exists without a state file / `merged_snapshot_ids`: nothing says which
    snapshots it already holds, so merging into it could double-count. Rebuild instead."""


class SnapshotConflictError(ArchiveStateError):
    """The same snapshot id appears in two sources with different content."""


class SourceNotIndexedError(ArchiveStateError):
    """A source lacks a `snapshot_id` index on a skill table (an insurance copy that was not prepared)."""


class Source(NamedTuple):
    name: str
    path: Path


@dataclass
class _SnapState:
    """What one source says about one snapshot."""
    snapshot_id: int
    created_at: str
    days: Optional[int]
    status: str  # finished | running | stale_running | failed | abandoned | none
    stat_rows: int = 0
    passive_rows: int = 0
    pop_rows: int = 0

    @property
    def has_rows(self) -> bool:
        return self.stat_rows + self.passive_rows > 0

    def fingerprint(self) -> tuple:
        return (self.created_at, self.days, self.stat_rows, self.passive_rows, self.pop_rows)


@dataclass
class MergeReport:
    merged: list[int] = field(default_factory=list)
    merged_from: dict[int, str] = field(default_factory=dict)
    already_merged: int = 0
    # finished somewhere but no source has its rows (rows aged out and no copy covers it): permanent loss
    unavailable: list[int] = field(default_factory=list)
    stale_running: list[int] = field(default_factory=list)   # 'running' older than resume_max_age_hours, treated as abandoned
    active_running: list[int] = field(default_factory=list)  # 'running' and young: left for the next cycle
    not_finished: list[int] = field(default_factory=list)    # failed / abandoned / no run row
    # snapshot ids each source can still supply rows for (finished + rows), for the "covered through" report
    source_snapshots: dict[str, list[int]] = field(default_factory=dict)


# ---- state file -------------------------------------------------------------------

def open_state(path: Path, game: str, league: str, create: bool) -> sqlite3.Connection:
    """The accumulation file. `create=False` on an existing file verifies it (quick_check, schema, game/league)
    and raises `ArchiveStateError` on anything off -- it never recreates a damaged file."""
    path = Path(path)
    exists = path.exists()
    if not exists and not create:
        raise ArchiveStateError(f"state file {path} does not exist")
    if exists and create:
        raise ArchiveStateError(f"refusing to create {path}: it already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA cache_size=-2000")
        if not exists:
            conn.executescript(_STATE_DDL)
            conn.executemany("INSERT INTO meta VALUES (?, ?)", [
                ("schema_version", str(STATE_SCHEMA_VERSION)), ("game", game), ("league", league),
                ("created_at", _now_iso()),
            ])
            return conn
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.DatabaseError as e:
            raise ArchiveStateError(f"state file {path} is unreadable: {e}") from e
        if check is None or check[0] != "ok":
            raise ArchiveStateError(f"state file {path} failed quick_check: {check}")
        if meta.get("schema_version") != str(STATE_SCHEMA_VERSION):
            raise ArchiveStateError(f"state file {path} has schema_version {meta.get('schema_version')!r}")
        if (meta.get("game"), meta.get("league")) != (game, league):
            raise ArchiveStateError(f"state file {path} belongs to {meta.get('game')}/{meta.get('league')}")
        return conn
    except sqlite3.DatabaseError as e:
        conn.close()
        raise ArchiveStateError(f"state file {path} is unreadable: {e}") from e
    except BaseException:
        conn.close()
        raise


def backup_state(path: Path) -> Path:
    """Consistent copy of the state file next to it (`<name>.bak`, replaced atomically), made through SQLite's
    backup API before a merge changes anything."""
    path = Path(path)
    bak = path.with_name(path.name + ".bak")
    tmp = path.with_name(path.name + ".bak.tmp")
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    with open(tmp, "r+b") as fh:  # fsync needs a writable descriptor on Windows
        os.fsync(fh.fileno())
    os.replace(tmp, bak)
    return bak


def merged_snapshot_ids(state: sqlite3.Connection) -> list[int]:
    return [r[0] for r in state.execute("SELECT snapshot_id FROM merged_snapshots ORDER BY snapshot_id")]


# ---- reading a source -------------------------------------------------------------

_SKILL_TABLES = ("skill_stat_counts", "skill_passive_counts", "skill_populations")


def _require_snapshot_indexes(conn: sqlite3.Connection, source: Source) -> None:
    for table in _SKILL_TABLES:
        found = False
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
            first = conn.execute(f"PRAGMA index_info({row[1]})").fetchone()
            if first is not None and first[2] == "snapshot_id":
                found = True
                break
        if not found:
            raise SourceNotIndexedError(
                f"source {source.name} ({source.path}) has no snapshot_id index on {table}; "
                "run tools/prepare_archive_copy.py on the extracted copy first"
            )


def _scan_source(
    conn: sqlite3.Connection, game: str, league: str, cutoff: str,
) -> dict[int, _SnapState]:
    """Every skill-centric snapshot of the league with its run status. A snapshot may have several `skill_runs`
    rows; 'finished' wins, otherwise the newest. 'running' older than the cutoff is reported as 'stale_running'."""
    rows = conn.execute(
        """SELECT s.id, s.created_at, s.days_since_league_start, sr.status, sr.started_at
           FROM snapshots s LEFT JOIN skill_runs sr ON sr.snapshot_id = s.id
           WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
           ORDER BY s.id, sr.id""",
        (game, league, COLLECTION_METHOD_SKILL),
    ).fetchall()
    out: dict[int, _SnapState] = {}
    for sid, created_at, days, status, started_at in rows:
        eff = status or "none"
        if eff == "running" and (started_at or "") < cutoff:
            eff = "stale_running"
        prev = out.get(sid)
        if prev is None or (prev.status != "finished" and eff != "none"):
            out[sid] = _SnapState(sid, created_at, days, eff)
    return out


def _fill_counts(conn: sqlite3.Connection, st: _SnapState) -> None:
    q = "SELECT COUNT(*) FROM {} WHERE snapshot_id = ?"
    st.stat_rows = conn.execute(q.format("skill_stat_counts"), (st.snapshot_id,)).fetchone()[0]
    st.passive_rows = conn.execute(q.format("skill_passive_counts"), (st.snapshot_id,)).fetchone()[0]
    st.pop_rows = conn.execute(q.format("skill_populations"), (st.snapshot_id,)).fetchone()[0]


# ---- merge --------------------------------------------------------------------------

def _stream(conn: sqlite3.Connection, sql: str, params: tuple, shape) -> Iterator[tuple]:
    cur = conn.execute(sql, params)
    try:
        while True:
            batch = cur.fetchmany(_BATCH)
            if not batch:
                return
            for row in batch:
                yield shape(row)
    finally:
        cur.close()


def _merge_snapshot(state: sqlite3.Connection, src: sqlite3.Connection, st: _SnapState, source_name: str) -> None:
    """Adds one snapshot's rows to the running totals and records its id -- one transaction, all or nothing."""
    bucket = _age_bucket(st.days)
    sid = st.snapshot_id
    counted = {"stat": 0, "passive": 0, "pop": 0}

    def counting(kind: str, it: Iterator[tuple]) -> Iterator[tuple]:
        for row in it:
            counted[kind] += 1
            yield row

    state.execute("BEGIN IMMEDIATE")
    try:
        if state.execute("SELECT 1 FROM merged_snapshots WHERE snapshot_id = ?", (sid,)).fetchone():
            raise ArchiveStateError(f"snapshot {sid} is already recorded as merged")
        state.executemany(_UPSERT_STAT, counting("stat", _stream(
            src, """SELECT skill_type, skill, level_bracket, slot, stat_id, mod_kind, count, value_min, value_max
                    FROM skill_stat_counts WHERE snapshot_id = ?""", (sid,),
            lambda r: (r[0], r[1], r[2], bucket, r[3], r[4], r[5], r[6], r[7], r[8]))))
        state.executemany(_UPSERT_PASSIVE, counting("passive", _stream(
            src, "SELECT skill_type, skill, level_bracket, keypassive, count FROM skill_passive_counts WHERE snapshot_id = ?",
            (sid,), lambda r: (r[0], r[1], r[2], bucket, r[3], r[4]))))
        state.executemany(_UPSERT_POP, counting("pop", _stream(
            src, "SELECT skill_type, skill, level_bracket, population FROM skill_populations WHERE snapshot_id = ?",
            (sid,), lambda r: (r[0], r[1], r[2], bucket, r[3]))))
        if (counted["stat"], counted["passive"], counted["pop"]) != (st.stat_rows, st.passive_rows, st.pop_rows):
            raise ArchiveStateError(
                f"snapshot {sid} changed in {source_name} while it was being read "
                f"(counted {counted}, expected {st.stat_rows}/{st.passive_rows}/{st.pop_rows}); nothing was merged"
            )
        state.execute(
            "INSERT INTO merged_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (sid, source_name, st.created_at, st.days, st.stat_rows, st.passive_rows, st.pop_rows, _now_iso()),
        )
        state.execute("COMMIT")
    except BaseException:
        state.execute("ROLLBACK")
        raise


def merge_snapshots(
    state: sqlite3.Connection, sources: Sequence[Source], game: str, league: str,
    resume_max_age_hours: float = 48.0, now: Optional[datetime] = None,
) -> MergeReport:
    """Merge every not-yet-merged finished snapshot into `state`, reading only from `sources` (all `mode=ro`),
    live database first. See the module docstring for the rules."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=resume_max_age_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = MergeReport()
    already = set(merged_snapshot_ids(state))
    backed_up = False

    conns: dict[str, sqlite3.Connection] = {}
    scans: dict[str, dict[int, _SnapState]] = {}
    try:
        for src in sources:
            conn = open_readonly(src.path)
            conns[src.name] = conn
            _require_snapshot_indexes(conn, src)
            scans[src.name] = _scan_source(conn, game, league, cutoff)

        all_ids = sorted({sid for scan in scans.values() for sid in scan})
        # a snapshot's status is judged by the *first* source that knows it -- the live database is the truth about
        # runs; a copy may have caught a run half-way, which is exactly why it must not supply rows for it
        for sid in all_ids:
            finished_somewhere = any(scan.get(sid) is not None and scan[sid].status == "finished" for scan in scans.values())
            first = next(scans[s.name][sid] for s in sources if sid in scans[s.name])
            if not finished_somewhere:
                {"stale_running": report.stale_running, "running": report.active_running}.get(
                    first.status, report.not_finished).append(sid)
                continue
            if sid in already:
                report.already_merged += 1
                continue
            candidates: list[tuple[str, _SnapState]] = []
            for s in sources:
                st = scans[s.name].get(sid)
                if st is None or st.status != "finished":
                    continue
                _fill_counts(conns[s.name], st)
                if st.has_rows:
                    candidates.append((s.name, st))
            for name, st in candidates:
                report.source_snapshots.setdefault(name, []).append(sid)
            if not candidates:
                report.unavailable.append(sid)
                continue
            base_name, base = candidates[0]
            for other_name, other in candidates[1:]:
                if other.fingerprint() != base.fingerprint():
                    raise SnapshotConflictError(
                        f"snapshot {sid} differs between {base_name} {base.fingerprint()} and "
                        f"{other_name} {other.fingerprint()} (created_at, days, stat/passive/population rows)"
                    )
            if not backed_up:
                backup_state(_state_path(state))  # first real change of this run: keep the state as it was
                backed_up = True
            _merge_snapshot(state, conns[base_name], base, base_name)
            report.merged.append(sid)
            report.merged_from[sid] = base_name
        # snapshots already merged and still servable are also "covered"; report them for covered-through math
        for name, scan in scans.items():
            seen = set(report.source_snapshots.get(name, []))
            for sid, st in scan.items():
                if sid in already and st.status == "finished" and sid not in seen:
                    _fill_counts(conns[name], st)
                    if st.has_rows:
                        report.source_snapshots.setdefault(name, []).append(sid)
    finally:
        for c in conns.values():
            c.close()
    return report


def _state_path(state: sqlite3.Connection) -> Path:
    return Path(state.execute("PRAGMA database_list").fetchone()[2])


# ---- render ---------------------------------------------------------------------------

def render_skills_file(
    state: sqlite3.Connection, game: str, league: str, out_path: Path, exported_at: Optional[str] = None,
) -> dict[str, Any]:
    """Writes `{game}-{slug}.skills.json` from the state (atomically). Streams: three ordered cursors over the
    state tables, one leaf in memory at a time."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    ids = merged_snapshot_ids(state)
    # populations of (skill, bracket, bucket) keys that have no stat/passive row never become a leaf; they are listed
    # at the end of the file so a state restored from it keeps them (a later snapshot may add rows to such a key)
    empty_pops = [list(r) for r in state.execute(
        """SELECT p.skill_type, p.skill, p.level_bracket, p.bucket, p.population FROM pop_acc p
           WHERE NOT EXISTS (SELECT 1 FROM stat_acc s WHERE s.skill_type = p.skill_type AND s.skill = p.skill
                             AND s.level_bracket = p.level_bracket AND s.bucket = p.bucket)
             AND NOT EXISTS (SELECT 1 FROM passive_acc s WHERE s.skill_type = p.skill_type AND s.skill = p.skill
                             AND s.level_bracket = p.level_bracket AND s.bucket = p.bucket)
           ORDER BY 1, 2, 3, 4""")]
    stat_cur = state.execute(
        """SELECT skill_type, skill, level_bracket, bucket, slot, stat_id, mod_kind, count, value_min, value_max
           FROM stat_acc ORDER BY skill_type, skill, level_bracket, bucket, slot, count DESC, stat_id, mod_kind""")
    pass_cur = state.execute(
        """SELECT skill_type, skill, level_bracket, bucket, keypassive, count
           FROM passive_acc ORDER BY skill_type, skill, level_bracket, bucket, count DESC, keypassive""")
    pop_cur = state.execute(
        "SELECT skill_type, skill, level_bracket, bucket, population FROM pop_acc ORDER BY skill_type, skill, level_bracket, bucket")

    def rows(cur) -> Iterator[tuple]:
        while True:
            batch = cur.fetchmany(_BATCH)
            if not batch:
                return
            yield from batch

    leaves = 0
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
            header = {
                "schema_version": SCHEMA_VERSION, "kind": "skills", "game": game, "league": league,
                "league_slug": sanitize_league_filename(league), "exported_at": exported_at or _now_iso(),
                "age_buckets": ["1-21", "22-60", "61+", "unknown"], "merged_snapshot_ids": ids,
            }
            fh.write(json.dumps(header, ensure_ascii=False, separators=(",", ":"))[:-1])
            fh.write(',"skills":')
            writer = _NestedJsonWriter(fh)
            writer.begin()
            for path, leaf in assemble_skill_leaves(rows(stat_cur), rows(pass_cur), rows(pop_cur)):
                writer.emit(path, leaf)
                leaves += 1
            writer.end()
            fh.write(',"populations_without_rows":' + json.dumps(empty_pops, ensure_ascii=False, separators=(",", ":")))
            fh.write("}")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    finally:
        for cur in (stat_cur, pass_cur, pop_cur):
            cur.close()
    return {"bytes": out_path.stat().st_size, "leaves": leaves, "snapshots": len(ids)}


# ---- disaster recovery --------------------------------------------------------------------

def restore_state_from_skills_file(json_path: Path, state_path: Path) -> dict[str, int]:
    """Rebuild a state file from a rendered `{game}-{slug}.skills.json` (e.g. the copy on Firebase). Loads the
    whole JSON, so it is a dev-machine tool, not for the 460MB VM. Lossless for every count, value range and
    population (those of keys without any stat/passive row travel in `populations_without_rows`; a file rendered
    before that field existed restores without them). What a rendering cannot carry is the per-snapshot
    metadata (only the ids survive)."""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    ids = data.get("merged_snapshot_ids")
    if not isinstance(ids, list):
        raise LegacyArchiveError(f"{json_path} has no merged_snapshot_ids; it cannot seed a state file")
    state = open_state(Path(state_path), data["game"], data["league"], create=True)
    counts = {"leaves": 0, "stat_rows": 0, "passive_rows": 0}
    try:
        state.execute("BEGIN IMMEDIATE")
        for skill_type, skills in data["skills"].items():
            for skill, brackets in skills.items():
                for bracket, buckets in brackets.items():
                    for bucket, leaf in buckets.items():
                        counts["leaves"] += 1
                        state.execute("INSERT INTO pop_acc VALUES (?,?,?,?,?)",
                                      (skill_type, skill, bracket, bucket, leaf["population"]))
                        for slot, rows_ in leaf["mods"].items():
                            for r in rows_:
                                state.execute("INSERT INTO stat_acc VALUES (?,?,?,?,?,?,?,?,?,?)", (
                                    skill_type, skill, bracket, bucket, slot, r["stat_id"], r["mod_kind"],
                                    r["count"], r["value_min"], r["value_max"]))
                                counts["stat_rows"] += 1
                        for r in leaf["passives"]:
                            state.execute("INSERT INTO passive_acc VALUES (?,?,?,?,?,?)",
                                          (skill_type, skill, bracket, bucket, r["keypassive"], r["count"]))
                            counts["passive_rows"] += 1
        for skill_type, skill, bracket, bucket, population in data.get("populations_without_rows", []):
            state.execute("INSERT INTO pop_acc VALUES (?,?,?,?,?)", (skill_type, skill, bracket, bucket, population))
        state.executemany(
            "INSERT INTO merged_snapshots (snapshot_id, source, merged_at) VALUES (?, 'restored', ?)",
            [(i, _now_iso()) for i in ids])
        state.execute("COMMIT")
    except BaseException:
        state.execute("ROLLBACK")
        state.close()
        Path(state_path).unlink(missing_ok=True)
        raise
    state.close()
    return counts
