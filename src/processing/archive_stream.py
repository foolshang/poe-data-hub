"""Streaming per-league archive builder (archive rewrite, step 1 of 2).

Same output *content* as `archive_export.build_league_archive(previous=None)`
-- same four dimensions (`mod_counts`, `skill_mod_counts`, `passives`,
`skills`), same age buckets, same population-dedup rules, same row fields --
but built without ever holding a league in Python memory:

- every sum / min / max is a SQL `GROUP BY` (SQLite spills its sort to temp
  files, it does not need the groups in RAM);
- the result is read through cursors ordered by the output key, so each output
  leaf (one list of stat rows, or one skill's record) is assembled, written and
  dropped before the next one is read;
- the file is written leaf by leaf (`_NestedJsonWriter`) to `<path>.tmp` and
  then `os.replace`d, so a crash never leaves a half-written archive.

Why it exists: the old builder pulled `iter_*_for_league` results into Python
lists and then into nested dicts. With PoE1's ~7.5M `skill_stat_counts` rows
that was ~2.6GB and took the 955MB deploy VM down on 2026-10-15 (CLAUDE.md).

What is deliberately *not* byte-identical to the old builder: key order and
the order of rows that tie on `count`. The old builder emitted keys in
first-seen order and tie rows in first-seen order, which depends on SQLite's
unspecified row order; this one sorts keys and breaks ties by
(stat_id, mod_kind), so the file is deterministic. Everything else (the
parsed JSON, the multiset of rows in every list, `count`-descending order)
is checked equal against the old builder in tests/test_archive_stream.py.

Read-only: it opens the database with `mode=ro` and never writes to it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

from src.processing.archive_export import SCHEMA_VERSION, _now_iso
from src.storage.local_db import COLLECTION_METHOD_LEGACY, COLLECTION_METHOD_SKILL
from src.storage.publisher import sanitize_league_filename

log = logging.getLogger(__name__)

_FETCH_BATCH = 5000

# Same bounds as archive_export._age_bucket ("1-21", "22-60", "61+", else "unknown").
_BUCKET_SQL = (
    "CASE WHEN {d} IS NULL OR {d} < 1 THEN 'unknown' "
    "WHEN {d} <= 21 THEN '1-21' WHEN {d} <= 60 THEN '22-60' ELSE '61+' END"
)


def _bucket(expr: str) -> str:
    return _BUCKET_SQL.format(d=expr)


def open_readonly(path: Path) -> sqlite3.Connection:
    """Read-only connection with a small page cache. `mode=ro` is enforced by
    SQLite itself: any write attempt raises, it is not just convention."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA cache_size=-1000")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple) -> Iterator[tuple]:
    cur = conn.execute(sql, params)
    try:
        while True:
            batch = cur.fetchmany(_FETCH_BATCH)
            if not batch:
                return
            yield from batch
    finally:
        cur.close()


def _grouped(rows: Iterator[tuple], key_len: int) -> Iterator[tuple[tuple, list[tuple]]]:
    """Consecutive rows sharing their first `key_len` columns -> (key, rows).
    The query must already be ordered by that key."""
    current: Optional[tuple] = None
    bucket: list[tuple] = []
    for row in rows:
        key = row[:key_len]
        if current is None or key != current:
            if current is not None:
                yield current, bucket
            current, bucket = key, []
        bucket.append(row)
    if current is not None:
        yield current, bucket


class _NestedJsonWriter:
    """Writes `{k1:{k2:{...leaf}}}` incrementally. `emit` must be called with
    paths in sorted order, so equal prefixes are adjacent."""

    def __init__(self, fh):
        self._fh = fh
        self._path: tuple[str, ...] = ()
        self._started = False  # anything written inside the current object yet?
        self._open_depth = 0

    def begin(self) -> None:
        self._fh.write("{")
        self._open_depth = 0
        self._path = ()
        self._started = False

    def emit(self, path: tuple[str, ...], leaf: Any) -> None:
        common = 0
        while common < len(self._path) and common < len(path) - 1 and self._path[common] == path[common]:
            common += 1
        # close objects that are no longer on the path
        for _ in range(len(self._path) - common):
            self._fh.write("}")
        if self._started or self._path:
            self._fh.write(",")
        # open the objects of the new path (all but the leaf key)
        for key in path[common:-1]:
            self._fh.write(json.dumps(key, ensure_ascii=False) + ":{")
        self._fh.write(json.dumps(path[-1], ensure_ascii=False) + ":")
        self._fh.write(json.dumps(leaf, ensure_ascii=False, separators=(",", ":")))
        self._path = path[:-1]
        self._started = True

    def end(self) -> None:
        for _ in range(len(self._path)):
            self._fh.write("}")
        self._fh.write("}")
        self._path = ()


def _stat_row(stat_id: Any, mod_kind: Any, count: int, v_min: Any, v_max: Any) -> dict[str, Any]:
    return {"stat_id": stat_id, "mod_kind": mod_kind, "count": count, "value_min": v_min, "value_max": v_max}


def _with_usage(rows: list[dict[str, Any]], population: int) -> list[dict[str, Any]]:
    for r in rows:
        r["population"] = population
        r["usage_pct"] = round(r["count"] / population * 100, 2) if population else 0.0
    return rows


# ---- legacy dimensions: mod_counts / skill_mod_counts / passives ----------------

def _iter_mod_counts(conn: sqlite3.Connection, game: str, league: str) -> Iterator[tuple[tuple[str, ...], list]]:
    sql = f"""
        SELECT mc.slot, mc.archetype, {_bucket('s.days_since_league_start')} AS b, mc.stat_id, mc.mod_kind,
               SUM(mc.count) AS c, MIN(mc.value_min), MAX(mc.value_max)
        FROM mod_counts mc JOIN snapshots s ON s.id = mc.snapshot_id
        WHERE s.game = ? AND s.league = ?
        GROUP BY mc.slot, mc.archetype, b, mc.stat_id, mc.mod_kind
        ORDER BY mc.slot, mc.archetype, b, c DESC, mc.stat_id, mc.mod_kind"""
    for key, rows in _grouped(_rows(conn, sql, (game, league)), 3):
        yield key, [_stat_row(r[3], r[4], r[5], r[6], r[7]) for r in rows]


def _iter_with_population(
    conn: sqlite3.Connection, stat_sql: str, pop_sql: str, params: tuple, key_len: int,
    make_row,
) -> Iterator[tuple[tuple[str, ...], list]]:
    """Merge-join of a stat query and a population query that share the same
    leading key columns and the same ORDER BY on them (so neither is held in
    memory). `make_row(row) -> dict` builds one output row from a stat row."""
    pops = _rows(conn, pop_sql, params)
    pending = next(pops, None)
    for key, rows in _grouped(_rows(conn, stat_sql, params), key_len):
        while pending is not None and pending[:key_len] < key:
            pending = next(pops, None)
        population = pending[key_len] if pending is not None and pending[:key_len] == key else 0
        yield key, _with_usage([make_row(r) for r in rows], population)


def _iter_skill_mod_counts(conn: sqlite3.Connection, game: str, league: str) -> Iterator[tuple[tuple[str, ...], list]]:
    bucket = _bucket("smc.days_since_league_start")
    stat_sql = f"""
        SELECT smc.slot, smc.main_skill, smc.level_bracket, {bucket} AS b, smc.stat_id, smc.mod_kind,
               SUM(smc.count) AS c, MIN(smc.value_min), MAX(smc.value_max)
        FROM skill_mod_counts smc JOIN snapshots s ON s.id = smc.snapshot_id
        WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
        GROUP BY smc.slot, smc.main_skill, smc.level_bracket, b, smc.stat_id, smc.mod_kind
        ORDER BY smc.slot, smc.main_skill, smc.level_bracket, b, c DESC, smc.stat_id, smc.mod_kind"""
    # population: counted once per (snapshot, slot, skill, bracket, archetype) group, not once per stat row
    pop_sql = f"""
        SELECT slot, main_skill, level_bracket, b, SUM(p) FROM (
            SELECT smc.slot AS slot, smc.main_skill AS main_skill, smc.level_bracket AS level_bracket,
                   {bucket} AS b, MAX(smc.population) AS p
            FROM skill_mod_counts smc JOIN snapshots s ON s.id = smc.snapshot_id
            WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
            GROUP BY smc.snapshot_id, smc.slot, smc.main_skill, smc.level_bracket, smc.archetype, b)
        GROUP BY slot, main_skill, level_bracket, b
        ORDER BY slot, main_skill, level_bracket, b"""
    yield from _iter_with_population(
        conn, stat_sql, pop_sql, (game, league, COLLECTION_METHOD_LEGACY), 4,
        lambda r: _stat_row(r[4], r[5], r[6], r[7], r[8]),
    )


def _iter_passives(conn: sqlite3.Connection, game: str, league: str) -> Iterator[tuple[tuple[str, ...], list]]:
    bucket = _bucket("kpc.days_since_league_start")
    stat_sql = f"""
        SELECT kpc.main_skill, kpc.level_bracket, {bucket} AS b, kpc.keypassive, SUM(kpc.count) AS c
        FROM keypassive_counts kpc JOIN snapshots s ON s.id = kpc.snapshot_id
        WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
        GROUP BY kpc.main_skill, kpc.level_bracket, b, kpc.keypassive
        ORDER BY kpc.main_skill, kpc.level_bracket, b, c DESC, kpc.keypassive"""
    pop_sql = f"""
        SELECT main_skill, level_bracket, b, SUM(p) FROM (
            SELECT kpc.main_skill AS main_skill, kpc.level_bracket AS level_bracket, {bucket} AS b,
                   MAX(kpc.population) AS p
            FROM keypassive_counts kpc JOIN snapshots s ON s.id = kpc.snapshot_id
            WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
            GROUP BY kpc.snapshot_id, kpc.main_skill, kpc.level_bracket, kpc.archetype, b)
        GROUP BY main_skill, level_bracket, b
        ORDER BY main_skill, level_bracket, b"""
    yield from _iter_with_population(
        conn, stat_sql, pop_sql, (game, league, COLLECTION_METHOD_LEGACY), 3,
        lambda r: {"keypassive": r[3], "count": r[4]},
    )


# ---- skill-centric dimension -----------------------------------------------------

def _iter_skill_leaves(conn: sqlite3.Connection, game: str, league: str) -> Iterator[tuple[tuple[str, ...], dict]]:
    """The `skills` dimension straight from the live skill-centric tables
    (every `skill_top100_v1` snapshot, finished or not). The accumulating
    archive in `archive_skills.py` feeds `assemble_skill_leaves` from its own
    state file instead."""
    bucket = _bucket("ssc.days_since_league_start")
    stat_sql = f"""
        SELECT ssc.skill_type, ssc.skill, ssc.level_bracket, {bucket} AS b, ssc.slot, ssc.stat_id, ssc.mod_kind,
               SUM(ssc.count) AS c, MIN(ssc.value_min), MAX(ssc.value_max)
        FROM skill_stat_counts ssc JOIN snapshots s ON s.id = ssc.snapshot_id
        WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
        GROUP BY ssc.skill_type, ssc.skill, ssc.level_bracket, b, ssc.slot, ssc.stat_id, ssc.mod_kind
        ORDER BY ssc.skill_type, ssc.skill, ssc.level_bracket, b, ssc.slot, c DESC, ssc.stat_id, ssc.mod_kind"""
    passive_bucket = _bucket("spc.days_since_league_start")
    passive_sql = f"""
        SELECT spc.skill_type, spc.skill, spc.level_bracket, {passive_bucket} AS b, spc.keypassive, SUM(spc.count) AS c
        FROM skill_passive_counts spc JOIN snapshots s ON s.id = spc.snapshot_id
        WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
        GROUP BY spc.skill_type, spc.skill, spc.level_bracket, b, spc.keypassive
        ORDER BY spc.skill_type, spc.skill, spc.level_bracket, b, c DESC, spc.keypassive"""
    # Population of a leaf = sum of its snapshots' own populations, each counted once. Only snapshots that have
    # at least one stat or passive row take part, as in the old builder (it learnt a snapshot's age from those rows).
    pop_bucket = _bucket("s.days_since_league_start")
    pop_sql = f"""
        SELECT sp.skill_type, sp.skill, sp.level_bracket, {pop_bucket} AS b, SUM(sp.population)
        FROM skill_populations sp JOIN snapshots s ON s.id = sp.snapshot_id
        WHERE s.game = ? AND s.league = ? AND s.collection_method = ?
          AND (EXISTS (SELECT 1 FROM skill_stat_counts x WHERE x.snapshot_id = sp.snapshot_id)
               OR EXISTS (SELECT 1 FROM skill_passive_counts y WHERE y.snapshot_id = sp.snapshot_id))
        GROUP BY sp.skill_type, sp.skill, sp.level_bracket, b
        ORDER BY sp.skill_type, sp.skill, sp.level_bracket, b"""
    params = (game, league, COLLECTION_METHOD_SKILL)
    yield from assemble_skill_leaves(
        _rows(conn, stat_sql, params), _rows(conn, passive_sql, params), _rows(conn, pop_sql, params),
    )


def assemble_skill_leaves(
    stat_rows: Iterator[tuple], passive_rows: Iterator[tuple], pop_rows: Iterator[tuple],
) -> Iterator[tuple[tuple[str, ...], dict]]:
    """One (skill_type, skill, bracket, bucket) record at a time, three
    ordered row streams merge-joined on that key. All three must be ordered
    by (skill_type, skill, level_bracket, bucket) and:
    - `stat_rows`: key(4), slot, stat_id, mod_kind, count, v_min, v_max -- within a
      slot ordered count DESC, stat_id, mod_kind (grouped by slot);
    - `passive_rows`: key(4), keypassive, count -- ordered count DESC, keypassive;
    - `pop_rows`: key(4), population."""

    def stat_leaves() -> Iterator[tuple[tuple, dict[str, list]]]:
        current_key: Optional[tuple] = None
        mods: dict[str, list] = {}
        for key, rows in _grouped(stat_rows, 5):
            leaf_key, slot = key[:4], key[4]
            if current_key is not None and leaf_key != current_key:
                yield current_key, mods
                mods = {}
            current_key = leaf_key
            mods[slot] = [_stat_row(r[5], r[6], r[7], r[8], r[9]) for r in rows]
        if current_key is not None:
            yield current_key, mods

    def passive_leaves() -> Iterator[tuple[tuple, list]]:
        for key, rows in _grouped(passive_rows, 4):
            yield key, [{"keypassive": r[4], "count": r[5]} for r in rows]

    pops = iter(pop_rows)
    pop_pending = next(pops, None)
    stats, passives = stat_leaves(), passive_leaves()
    s, p = next(stats, None), next(passives, None)
    while s is not None or p is not None:
        key = min(x[0] for x in (s, p) if x is not None)
        mods = s[1] if s is not None and s[0] == key else {}
        kps = p[1] if p is not None and p[0] == key else []
        if s is not None and s[0] == key:
            s = next(stats, None)
        if p is not None and p[0] == key:
            p = next(passives, None)
        while pop_pending is not None and pop_pending[:4] < key:
            pop_pending = next(pops, None)
        population = pop_pending[4] if pop_pending is not None and pop_pending[:4] == key else 0
        for slot_rows in mods.values():
            _with_usage(slot_rows, population)
        yield key, {"population": population, "mods": mods, "passives": _with_usage(kps, population)}


# ---- build ------------------------------------------------------------------------

def build_league_archive_to_file(
    db_path: Path, game: str, league: str, out_path: Path, exported_at: Optional[str] = None,
    include_skills: bool = True,
) -> dict[str, int]:
    """Writes the archive for (game, league) to `out_path` (atomically, via a
    `.tmp` sibling) and returns `{"bytes": ..., "leaves": {dimension: n}}`.
    `include_skills=False` writes only the three legacy dimensions -- the
    shape of the archive files that already exist (and are read by other
    apps); the `skills` dimension then lives in its own accumulating file
    (`archive_skills.py`)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    conn = open_readonly(db_path)
    leaves: dict[str, int] = {}
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
            header = {
                "schema_version": SCHEMA_VERSION, "game": game, "league": league,
                "league_slug": sanitize_league_filename(league), "exported_at": exported_at or _now_iso(),
                "age_buckets": ["1-21", "22-60", "61+", "unknown"],
            }
            fh.write(json.dumps(header, ensure_ascii=False, separators=(",", ":"))[:-1])
            dims = (
                ("mod_counts", _iter_mod_counts(conn, game, league)),
                ("skill_mod_counts", _iter_skill_mod_counts(conn, game, league)),
                ("passives", _iter_passives(conn, game, league)),
                *((("skills", _iter_skill_leaves(conn, game, league)),) if include_skills else ()),
            )
            for name, it in dims:
                fh.write("," + json.dumps(name) + ":")
                writer = _NestedJsonWriter(fh)
                writer.begin()
                n = 0
                for path, leaf in it:
                    writer.emit(path, leaf)
                    n += 1
                writer.end()
                leaves[name] = n
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
        conn.close()
    size = out_path.stat().st_size
    log.info("archive_stream: %s/%s -> %s (%.1f KB, leaves %s)", game, league, out_path, size / 1024, leaves)
    return {"bytes": size, "leaves": leaves}
