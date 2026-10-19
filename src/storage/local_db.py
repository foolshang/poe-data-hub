"""SQLite storage for meta snapshots (spec 6.5).

Four tables:
  snapshots        -- one row per meta job run. Kept forever.
  char_items       -- one row per fetched character, raw item JSON attached.
                       Deleted after 90 days (cleanup_old_char_items).
  mod_counts       -- aggregated mod frequency per (slot, archetype, stat_id).
                       Kept forever across leagues on purpose: meta shifts with
                       league age (early-league = res/life, late-league = damage),
                       so keeping mod_counts across leagues lets next league compare
                       against "last league's early days" (spec 6.5).
  skill_mod_counts -- skill-meta phase 2: a second, orthogonal dimension --
                       (game, slot, main_skill, level_bracket, archetype,
                       stat_id) -- also kept forever, see its own comment in
                       _SCHEMA below.

`snapshots.game` added phase 10 (PoE1 meta) via an explicit migration
(`_migrate`) rather than just adding it to `_SCHEMA`'s `CREATE TABLE IF NOT
EXISTS`, since that clause is a no-op on a table that already exists --
production already has real poe2-only snapshot rows from phases 1-9 that
need this new column added in place, not silently ignored. Existing rows
backfill to `'poe2'` (correct: every row inserted before this migration was
poe2, this project's only game with a meta pipeline until now).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

DEFAULT_DB_PATH = Path("data/poe_data_hub.sqlite3")

COLLECTION_METHOD_LEGACY = "legacy_slot_top100"
COLLECTION_METHOD_SKILL = "skill_top100_v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    league TEXT NOT NULL,
    days_since_league_start INTEGER,
    sample_total INTEGER NOT NULL,
    unique_characters_fetched INTEGER NOT NULL,
    fetch_failed INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS char_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    account TEXT NOT NULL,
    name TEXT NOT NULL,
    level INTEGER,
    class TEXT,
    archetype TEXT,
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_char_items_snapshot ON char_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_char_items_created_at ON char_items(created_at);

CREATE TABLE IF NOT EXISTS mod_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    slot TEXT NOT NULL,
    archetype TEXT NOT NULL,
    stat_id TEXT NOT NULL,
    mod_kind TEXT NOT NULL,
    count INTEGER NOT NULL,
    usage_pct REAL NOT NULL,
    rank_in_slot INTEGER NOT NULL,
    value_min REAL,
    value_max REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mod_counts_snapshot ON mod_counts(snapshot_id);

-- Skill-meta phase 2: a second, orthogonal dimension alongside mod_counts'
-- archetype-based one -- (game, slot, main_skill, level_bracket, stat_id),
-- covering gear and jewels. Kept forever across leagues, same as mod_counts
-- (never cleaned up by cleanup_old_char_items or any other job) -- meta
-- shifts with league age, so next league's early days benefit from
-- comparing against this league's, same reasoning as mod_counts' docstring.
-- `archetype` is denormalized onto every row (not just derivable from
-- main_skill) because Archetyper.classify's weapon-attack fallback depends
-- on the character's actual weapon, so the same skill can land in different
-- archetypes for different characters -- see aggregator.py's
-- aggregate_slot_by_skill docstring. This keeps every fallback tier (item
-- 12: skill+bracket -> skill -> archetype -> all) a query over this one
-- table's own columns, never needing a join back to mod_counts.
-- `days_since_league_start` is denormalized from snapshots too (not just
-- joinable) so an archive export (later phase) can read this table alone.
CREATE TABLE IF NOT EXISTS skill_mod_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game TEXT NOT NULL,
    slot TEXT NOT NULL,
    main_skill TEXT NOT NULL,
    level_bracket TEXT NOT NULL,
    archetype TEXT NOT NULL,
    stat_id TEXT NOT NULL,
    mod_kind TEXT NOT NULL,
    count INTEGER NOT NULL,
    population INTEGER NOT NULL,
    usage_pct REAL NOT NULL,
    rank_in_bracket INTEGER NOT NULL,
    value_min REAL,
    value_max REAL,
    days_since_league_start INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_mod_counts_snapshot ON skill_mod_counts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_skill_mod_counts_lookup
    ON skill_mod_counts(game, slot, main_skill, level_bracket);

-- Skill-meta phase 3, path A: notable/keystone passives only (poe.ninja's
-- builds-search "keypassives" column + dictionary -- confirmed live
-- 2026-09-17 this is a ~260-330-entry enumeration, NOT the full passive
-- tree's small stat nodes; see ninja_builds.py's module docstring and
-- CLAUDE.md). Same shape as skill_mod_counts minus `slot` (passives aren't
-- slot-specific) with `keypassive` in place of `stat_id`/`mod_kind`, kept
-- forever for the same reason mod_counts/skill_mod_counts are. `archetype`
-- is denormalized for the same 4-tier-fallback reason as skill_mod_counts.
CREATE TABLE IF NOT EXISTS keypassive_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game TEXT NOT NULL,
    main_skill TEXT NOT NULL,
    level_bracket TEXT NOT NULL,
    archetype TEXT NOT NULL,
    keypassive TEXT NOT NULL,
    count INTEGER NOT NULL,
    population INTEGER NOT NULL,
    usage_pct REAL NOT NULL,
    rank_in_bracket INTEGER NOT NULL,
    days_since_league_start INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_keypassive_counts_snapshot ON keypassive_counts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_keypassive_counts_lookup
    ON keypassive_counts(game, main_skill, level_bracket);

-- ===== Skill-centric collection (2026-09-21) ================================
-- Replaces the per-slot skill dimension above (skill_mod_counts /
-- keypassive_counts, fed by a first-gem *guess* -- see SPEC.md "Skill-centric
-- collection"): each skill is a poe.ninja list, its top-100 characters are
-- fetched, and a character is counted only into the list(s) it came from.

-- Shared persistent character cache, used by both the skill-centric job and
-- the legacy per-slot job: a fetched character is reused while its level is
-- unchanged, the league is the same and the row is younger than
-- skill_meta.collect.reuse_max_age_days. One row per (game, account, name).
-- `level` is the level in the stored detail; `list_level` is the level
-- poe.ninja's LIST row showed when it was fetched. Reuse compares the new list
-- row against `list_level`: the search index can lag the detail endpoint (a
-- character levels in between), and comparing against `level` would refetch
-- such a character on every run until the index catches up.
CREATE TABLE IF NOT EXISTS char_details (
    game TEXT NOT NULL,
    account TEXT NOT NULL,
    name TEXT NOT NULL,
    league TEXT NOT NULL,
    level INTEGER NOT NULL,
    list_level INTEGER,
    detail_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (game, account, name)
);
CREATE INDEX IF NOT EXISTS idx_char_details_fetched_at ON char_details(fetched_at);

-- One row per collection attempt. `status` = running | finished | failed |
-- abandoned. A running row is resumed (checkpoints below) instead of
-- restarting; `plan_json` freezes the skill list + ascendancy cut chosen at
-- start so a resume continues the same plan.
CREATE TABLE IF NOT EXISTS skill_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL,
    league TEXT NOT NULL,
    snapshot_id INTEGER REFERENCES snapshots(id),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    population_total INTEGER,
    plan_json TEXT NOT NULL,
    stats_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_skill_runs_game_status ON skill_runs(game, status);

-- List-level checkpoint: a list is 'done' only once all its members are
-- committed. `skill` is the base name (a Vaal variant folds into it),
-- `list_name` the poe.ninja name actually queried.
CREATE TABLE IF NOT EXISTS skill_run_lists (
    run_id INTEGER NOT NULL REFERENCES skill_runs(id),
    skill_type TEXT NOT NULL,
    skill TEXT NOT NULL,
    list_name TEXT NOT NULL,
    facet_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    server_total INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (run_id, skill_type, list_name)
);

CREATE TABLE IF NOT EXISTS skill_run_members (
    run_id INTEGER NOT NULL REFERENCES skill_runs(id),
    skill_type TEXT NOT NULL,
    skill TEXT NOT NULL,
    list_name TEXT NOT NULL,
    account TEXT NOT NULL,
    name TEXT NOT NULL,
    level INTEGER NOT NULL,
    class TEXT NOT NULL,
    keypassives_json TEXT NOT NULL,
    PRIMARY KEY (run_id, skill_type, list_name, account, name)
);
CREATE INDEX IF NOT EXISTS idx_skill_run_members_skill ON skill_run_members(run_id, skill_type, skill);

-- Results. Kept forever (like mod_counts). `population` = characters counted
-- for (skill, skill_type, bracket) -- the same base for every slot/stat of
-- that skill; `slot_holders` = how many of them wear >=1 item in `slot`.
-- Regular slots and jewels are both counted per character (a character counts
-- once for a stat in a slot even with two jewels of that base carrying it).
CREATE TABLE IF NOT EXISTS skill_stat_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game TEXT NOT NULL,
    skill_type TEXT NOT NULL,
    skill TEXT NOT NULL,
    level_bracket TEXT NOT NULL,
    slot TEXT NOT NULL,
    stat_id TEXT NOT NULL,
    mod_kind TEXT NOT NULL,
    count INTEGER NOT NULL,
    population INTEGER NOT NULL,
    slot_holders INTEGER NOT NULL,
    usage_pct REAL NOT NULL,
    value_min REAL,
    value_max REAL,
    days_since_league_start INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_stat_counts_snapshot ON skill_stat_counts(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_skill_stat_counts_lookup
    ON skill_stat_counts(game, skill_type, skill, level_bracket, slot);

CREATE TABLE IF NOT EXISTS skill_passive_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game TEXT NOT NULL,
    skill_type TEXT NOT NULL,
    skill TEXT NOT NULL,
    level_bracket TEXT NOT NULL,
    keypassive TEXT NOT NULL,
    count INTEGER NOT NULL,
    population INTEGER NOT NULL,
    usage_pct REAL NOT NULL,
    days_since_league_start INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skill_passive_counts_snapshot ON skill_passive_counts(snapshot_id);

-- Per (skill, type, bracket): who was counted and how big the skill is in the
-- league (`facet_count`, summed over folded Vaal lists). Also what a
-- skill-autocomplete list (`skills[]`) is built from.
CREATE TABLE IF NOT EXISTS skill_populations (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    game TEXT NOT NULL,
    skill_type TEXT NOT NULL,
    skill TEXT NOT NULL,
    level_bracket TEXT NOT NULL,
    population INTEGER NOT NULL,
    facet_count INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, skill_type, skill, level_bracket)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LocalDb:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # `timeout=30`: Python's own SQLITE_BUSY retry window -- learned the
        # hard way 2026-09-22, first live incremental skill run: a read-only
        # ad-hoc diagnostic query (a `dbstat` sweep over the then-1.5GB
        # char_items table, opened as a completely separate `sqlite3.connect`
        # outside this class, so job_lock never saw it) held a lock long
        # enough that the *default* 5s timeout wasn't enough, and a live
        # skill-centric run died mid-list with "database is locked" (see
        # CLAUDE.md). WAL journal mode is the real fix -- a writer's commit
        # no longer blocks concurrent readers (and vice versa) the way the
        # default rollback journal does -- kept alongside the longer timeout
        # as a second line of defense for any reader that isn't WAL-aware
        # (an old sqlite3 CLI, a tool that forces `PRAGMA journal_mode`
        # itself, etc). `journal_mode` is stored in the database file itself,
        # so this converts the file to WAL permanently on first connect, not
        # just for this process.
        self._conn = sqlite3.connect(db_path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(snapshots)")}
        if "game" not in cols:
            self._conn.execute("ALTER TABLE snapshots ADD COLUMN game TEXT NOT NULL DEFAULT 'poe2'")

        # Skill-meta phase 2: raw main_skill was never persisted before this
        # (see CLAUDE.md) -- existing char_items rows backfill to NULL, which
        # is correct: that skill data is genuinely unrecoverable for them, not
        # merely unmigrated. Only rows inserted after this column exists carry
        # a real value; --reprocess-char-items skips NULL rows rather than
        # guessing.
        char_items_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(char_items)")}
        if "main_skill" not in char_items_cols:
            self._conn.execute("ALTER TABLE char_items ADD COLUMN main_skill TEXT")

        # Skill-meta phase 3, path A: raw passiveSelection (real GGG tree
        # node IDs) stored verbatim starting now, same "can't retroactively
        # recover pre-migration rows" situation as main_skill above -- rows
        # inserted before this column exists backfill to NULL, not a guess.
        if "passive_selection_json" not in char_items_cols:
            self._conn.execute("ALTER TABLE char_items ADD COLUMN passive_selection_json TEXT")

        # Skill-centric collection (2026-09-21): every snapshot records HOW its
        # data was collected. Rows that exist before this column are all
        # 'legacy_slot_top100' -- top-100-per-slot samples whose `main_skill`
        # (char_items / skill_mod_counts / keypassive_counts) was the first gem
        # of poe.ninja's skill list, a guess that is NOT reliable as a main
        # skill. Kept, not deleted; archive/publish of skill-level data must
        # only read 'skill_top100_v1' snapshots.
        snapshot_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(snapshots)")}
        if "collection_method" not in snapshot_cols:
            self._conn.execute(
                "ALTER TABLE snapshots ADD COLUMN collection_method TEXT NOT NULL DEFAULT 'legacy_slot_top100'"
            )

    def close(self) -> None:
        self._conn.close()

    def insert_snapshot(
        self,
        game: str,
        league: str,
        days_since_league_start: Optional[int],
        sample_total: int,
        unique_characters_fetched: int,
        fetch_failed: int,
        snapshot_date: Optional[str] = None,
        collection_method: str = COLLECTION_METHOD_LEGACY,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO snapshots
               (snapshot_date, game, league, days_since_league_start, sample_total,
                unique_characters_fetched, fetch_failed, created_at, collection_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                game,
                league,
                days_since_league_start,
                sample_total,
                unique_characters_fetched,
                fetch_failed,
                _now_iso(),
                collection_method,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def insert_char_item(
        self,
        snapshot_id: int,
        account: str,
        name: str,
        level: int,
        char_class: str,
        archetype: str,
        items_raw: Any,
        main_skill: Optional[str] = None,
        passive_selection: Optional[list[int]] = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO char_items
               (snapshot_id, account, name, level, class, archetype, items_json, main_skill,
                passive_selection_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id, account, name, level, char_class, archetype,
                json.dumps(items_raw, ensure_ascii=False), main_skill,
                json.dumps(list(passive_selection), ensure_ascii=False) if passive_selection is not None else None,
                _now_iso(),
            ),
        )

    def insert_skill_mod_counts(
        self, snapshot_id: int, game: str, days_since_league_start: Optional[int], rows: list[dict[str, Any]],
    ) -> None:
        self._conn.executemany(
            """INSERT INTO skill_mod_counts
               (snapshot_id, game, slot, main_skill, level_bracket, archetype, stat_id, mod_kind,
                count, population, usage_pct, rank_in_bracket, value_min, value_max,
                days_since_league_start, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    snapshot_id, game, r["slot"], r["main_skill"], r["level_bracket"], r["archetype"],
                    r["stat_id"], r["mod_kind"], r["count"], r["population"], r["usage_pct"],
                    r["rank_in_bracket"], r["value_min"], r["value_max"], days_since_league_start,
                    _now_iso(),
                )
                for r in rows
            ],
        )
        self._conn.commit()

    def delete_skill_mod_counts_for_snapshot(self, snapshot_id: int) -> int:
        """Makes --reprocess-char-items (item 13) safe to re-run: replaces
        (not accumulates) a snapshot's skill_mod_counts rows. Returns rows
        deleted."""
        cur = self._conn.execute("DELETE FROM skill_mod_counts WHERE snapshot_id = ?", (snapshot_id,))
        self._conn.commit()
        return cur.rowcount

    def get_snapshot_days_since_league_start(self, snapshot_id: int) -> Optional[int]:
        row = self._conn.execute(
            "SELECT days_since_league_start FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        return row[0] if row else None

    def insert_keypassive_counts(
        self, snapshot_id: int, game: str, days_since_league_start: Optional[int], rows: list[dict[str, Any]],
    ) -> None:
        self._conn.executemany(
            """INSERT INTO keypassive_counts
               (snapshot_id, game, main_skill, level_bracket, archetype, keypassive,
                count, population, usage_pct, rank_in_bracket, days_since_league_start, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    snapshot_id, game, r["main_skill"], r["level_bracket"], r["archetype"], r["keypassive"],
                    r["count"], r["population"], r["usage_pct"], r["rank_in_bracket"], days_since_league_start,
                    _now_iso(),
                )
                for r in rows
            ],
        )
        self._conn.commit()

    def iter_keypassive_counts_for_league(self, game: str, league: str) -> list[tuple]:
        """Every keypassive_counts row ever recorded for (game, league),
        joined to its snapshot's days_since_league_start -- used by
        src/processing/archive_export.py, same pattern as
        iter_skill_mod_counts_for_league."""
        return self._conn.execute(
            """SELECT kpc.main_skill, kpc.level_bracket, kpc.archetype, kpc.keypassive,
                      kpc.count, kpc.population, kpc.snapshot_id, kpc.days_since_league_start
               FROM keypassive_counts kpc JOIN snapshots s ON s.id = kpc.snapshot_id
               WHERE s.game = ? AND s.league = ? AND s.collection_method = ?""",
            (game, league, COLLECTION_METHOD_LEGACY),
        ).fetchall()

    def query_skill_mod_counts(
        self,
        game: str,
        slot: str,
        main_skill: str,
        level_bracket: str,
        min_population: int,
        snapshot_ids: Optional[list[int]] = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        """4-tier fallback (item 12): skill+bracket -> skill (all brackets) ->
        archetype (any skill) -> all. Returns `(tier, rows)`, `tier` one of
        "skill_bracket"/"skill"/"archetype"/"all" -- the finest tier whose
        population met `min_population` (or "all" regardless, as the final
        floor -- callers decide what to do if even that's too thin). `rows`:
        [{"stat_id","mod_kind","count","population","usage_pct","value_min",
        "value_max"}, ...] ranked by count desc.

        `snapshot_ids`: restrict to these snapshots (e.g. "this run only", or
        "last N days" via a snapshots-table join by the caller); `None` means
        every kept-forever row for this game/slot ever inserted.

        Aggregates in Python, not SQL SUM/GROUP BY: `population` is an
        attribute of a (snapshot_id, slot, main_skill, level_bracket,
        archetype) *group*, repeated on every stat_id row in that group --
        summing it per stat_id in SQL would multiply it by however many
        distinct stat_ids that group has. Dataset sizes here (per-snapshot
        sample counts) make Python aggregation fine."""
        where = "game = ? AND slot = ?"
        params: list[Any] = [game, slot]
        if snapshot_ids is not None:
            if not snapshot_ids:
                return "skill_bracket", []
            where += f" AND snapshot_id IN ({','.join('?' * len(snapshot_ids))})"
            params += snapshot_ids

        all_rows = self._conn.execute(
            f"""SELECT snapshot_id, main_skill, level_bracket, archetype, stat_id, mod_kind,
                       count, population, value_min, value_max
                FROM skill_mod_counts WHERE {where}""",
            params,
        ).fetchall()

        # A skill isn't purely one archetype (see aggregate_slot_by_skill's
        # docstring -- weapon-dependent fallback) -- tier 3 falls back to
        # whichever archetype has the largest population among this skill's
        # own (snapshot, bracket, archetype) groups, not an arbitrary one.
        skill_group_population: dict[tuple, int] = {}
        for r in all_rows:
            if r[1] != main_skill:
                continue
            skill_group_population[(r[0], r[2], r[3])] = r[7]  # (snapshot_id, bracket, archetype) -> population
        archetype_population: dict[str, int] = defaultdict(int)
        for (_snap, _bracket, archetype), pop in skill_group_population.items():
            archetype_population[archetype] += pop
        target_archetype = max(archetype_population, key=archetype_population.get) if archetype_population else None

        tiers = [
            ("skill_bracket", lambda r: r[1] == main_skill and r[2] == level_bracket),
            ("skill", lambda r: r[1] == main_skill),
            ("archetype", lambda r: target_archetype is not None and r[3] == target_archetype),
            ("all", lambda r: True),
        ]
        last: tuple[str, list[dict[str, Any]]] = ("all", [])
        for tier_name, predicate in tiers:
            matching = [r for r in all_rows if predicate(r)]
            population, stat_rows = self._aggregate_rows(matching)
            last = (tier_name, stat_rows)
            if population >= min_population:
                return last
        return last

    @staticmethod
    def _aggregate_rows(rows: list[tuple]) -> tuple[int, list[dict[str, Any]]]:
        """`rows`: raw tuples from query_skill_mod_counts' SELECT. Returns
        (total population, stat rows ranked by count desc)."""
        seen_groups: set[tuple] = set()
        population = 0
        by_stat: dict[tuple[str, str], dict[str, Any]] = {}
        for snapshot_id, skill, bracket, archetype, stat_id, mod_kind, count, pop, v_min, v_max in rows:
            group_key = (snapshot_id, skill, bracket, archetype)
            if group_key not in seen_groups:
                seen_groups.add(group_key)
                population += pop
            entry = by_stat.setdefault(
                (stat_id, mod_kind), {"stat_id": stat_id, "mod_kind": mod_kind, "count": 0, "value_min": None, "value_max": None},
            )
            entry["count"] += count
            if v_min is not None:
                entry["value_min"] = v_min if entry["value_min"] is None else min(entry["value_min"], v_min)
            if v_max is not None:
                entry["value_max"] = v_max if entry["value_max"] is None else max(entry["value_max"], v_max)

        stat_rows = sorted(by_stat.values(), key=lambda r: r["count"], reverse=True)
        for r in stat_rows:
            r["population"] = population
            r["usage_pct"] = round(r["count"] / population * 100, 2) if population else 0.0
        return population, stat_rows

    def iter_char_items_with_main_skill(
        self, game: str, since_iso: Optional[str] = None,
    ) -> list[tuple[int, int, str, int, Any]]:
        """For --reprocess-char-items (item 13): rows that actually have
        main_skill populated (only ones inserted after this column started
        being written -- see the migration comment above), joined to their
        snapshot's game. Returns [(char_item_id, snapshot_id, main_skill,
        level, items_json_parsed), ...]."""
        where = "s.game = ? AND ci.main_skill IS NOT NULL"
        params: list[Any] = [game]
        if since_iso:
            where += " AND ci.created_at >= ?"
            params.append(since_iso)
        rows = self._conn.execute(
            f"""SELECT ci.id, ci.snapshot_id, ci.main_skill, ci.level, ci.items_json
                FROM char_items ci JOIN snapshots s ON s.id = ci.snapshot_id
                WHERE {where}""",
            params,
        ).fetchall()
        return [(r[0], r[1], r[2], r[3], json.loads(r[4])) for r in rows]

    def most_recent_league(self, game: str) -> Optional[str]:
        """The league of this game's most recently-inserted snapshot, or
        None if this game has no snapshots yet. Used both for league-change
        detection (compare against the league a new meta run just detected)
        and by the weekly archive job (re-export whatever's currently
        active, cheaply, as a safety net against losing more than a week of
        an in-progress league if the VM disappears)."""
        row = self._conn.execute(
            "SELECT league FROM snapshots WHERE game = ? ORDER BY created_at DESC LIMIT 1", (game,)
        ).fetchone()
        return row[0] if row else None

    def iter_skill_mod_counts_for_league(self, game: str, league: str) -> list[tuple]:
        """Every skill_mod_counts row ever recorded for (game, league),
        joined to its snapshot's days_since_league_start -- used by
        src/processing/archive_export.py. Reads mod_counts/skill_mod_counts
        (kept forever) rather than char_items (90-day retention) so a
        league that ended long ago can still be archived (spec skill-meta
        phase 5, item 20)."""
        # Filtered to legacy_slot_top100 snapshots -- a no-op today (the
        # skill-centric job never writes to this table) but explicit so a
        # league that later mixes both collection methods can never blend an
        # unreliable-skill legacy row into what looks like current data.
        return self._conn.execute(
            """SELECT smc.slot, smc.main_skill, smc.level_bracket, smc.archetype, smc.stat_id, smc.mod_kind,
                      smc.count, smc.population, smc.value_min, smc.value_max, smc.snapshot_id,
                      smc.days_since_league_start
               FROM skill_mod_counts smc JOIN snapshots s ON s.id = smc.snapshot_id
               WHERE s.game = ? AND s.league = ? AND s.collection_method = ?""",
            (game, league, COLLECTION_METHOD_LEGACY),
        ).fetchall()

    def iter_mod_counts_for_league(self, game: str, league: str) -> list[tuple]:
        """Every mod_counts row ever recorded for (game, league), joined to
        its snapshot's days_since_league_start. Unlike skill_mod_counts,
        mod_counts has no `population` column (that denominator only ever
        lived in the ephemeral published meta.json's per_slot_sample, never
        persisted per-row) -- archive_export.py deliberately does not
        recompute a usage_pct for this table's archived rows, since summing
        `count` across snapshots with no matching population would produce
        a number that looks precise but isn't."""
        return self._conn.execute(
            """SELECT mc.slot, mc.archetype, mc.stat_id, mc.mod_kind, mc.count, mc.value_min, mc.value_max,
                      s.days_since_league_start
               FROM mod_counts mc JOIN snapshots s ON s.id = mc.snapshot_id
               WHERE s.game = ? AND s.league = ?""",
            (game, league),
        ).fetchall()

    def insert_mod_counts(self, snapshot_id: int, rows: list[dict[str, Any]]) -> None:
        self._conn.executemany(
            """INSERT INTO mod_counts
               (snapshot_id, slot, archetype, stat_id, mod_kind, count, usage_pct,
                rank_in_slot, value_min, value_max, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    snapshot_id, r["slot"], r["archetype"], r["stat_id"], r["mod_kind"],
                    r["count"], r["usage_pct"], r["rank_in_slot"], r["value_min"], r["value_max"],
                    _now_iso(),
                )
                for r in rows
            ],
        )
        self._conn.commit()

    # ---- shared character cache ------------------------------------------

    def get_fresh_char_detail(
        self, game: str, account: str, name: str, league: str, level: int, max_age_days: float,
    ) -> Optional[dict[str, Any]]:
        """The stored detail JSON for (game, account, name) if it is still
        reusable: same league, same level as the list row that asked, and
        fetched within `max_age_days`. Otherwise None (caller refetches)."""
        row = self._conn.execute(
            "SELECT league, COALESCE(list_level, level), detail_json, fetched_at FROM char_details "
            "WHERE game = ? AND account = ? AND name = ?",
            (game, account, name),
        ).fetchone()
        if row is None or row[0] != league or row[1] != level:
            return None
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
        fetched = datetime.strptime(row[3], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        if fetched < cutoff:
            return None
        return json.loads(row[2])

    def get_char_detail_any(self, game: str, account: str, name: str) -> Optional[dict[str, Any]]:
        """Stored detail JSON regardless of league/level/age -- for the counting
        phase, which must use exactly what the fetch phase left behind."""
        row = self._conn.execute(
            "SELECT detail_json FROM char_details WHERE game = ? AND account = ? AND name = ?",
            (game, account, name),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def upsert_char_detail(
        self, game: str, league: str, detail: dict[str, Any], list_level: Optional[int] = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO char_details (game, account, name, league, level, list_level, detail_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game, account, name) DO UPDATE SET
                 league = excluded.league, level = excluded.level, list_level = excluded.list_level,
                 detail_json = excluded.detail_json, fetched_at = excluded.fetched_at""",
            (
                game, detail["account"], detail["name"], league, int(detail["level"]), list_level,
                json.dumps(detail, ensure_ascii=False), _now_iso(),
            ),
        )
        self._conn.commit()

    def cleanup_old_char_details(self, older_than_days: int) -> int:
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - older_than_days * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute("DELETE FROM char_details WHERE fetched_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount

    # ---- skill-centric run checkpoints -----------------------------------

    def find_resumable_skill_run(self, game: str, league: str, max_age_hours: float) -> Optional[dict[str, Any]]:
        """The newest still-'running' run for (game, league) started within
        `max_age_hours`; older or other-league 'running' rows are marked
        'abandoned' (they can never be resumed) so they don't linger."""
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - max_age_hours * 3600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.execute(
            "UPDATE skill_runs SET status = 'abandoned', finished_at = ? "
            "WHERE game = ? AND status = 'running' AND (started_at < ? OR league != ?)",
            (_now_iso(), game, cutoff, league),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id, snapshot_id, started_at, population_total, plan_json FROM skill_runs "
            "WHERE game = ? AND league = ? AND status = 'running' ORDER BY id DESC LIMIT 1",
            (game, league),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "snapshot_id": row[1], "started_at": row[2],
            "population_total": row[3], "plan": json.loads(row[4]),
        }

    def create_skill_run(
        self, game: str, league: str, snapshot_id: int, population_total: int, plan: dict[str, Any],
        lists: list[dict[str, Any]],
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO skill_runs (game, league, snapshot_id, status, started_at, population_total, plan_json)
               VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (game, league, snapshot_id, _now_iso(), population_total, json.dumps(plan, ensure_ascii=False)),
        )
        run_id = cur.lastrowid
        self._conn.executemany(
            """INSERT INTO skill_run_lists (run_id, skill_type, skill, list_name, facet_count)
               VALUES (?, ?, ?, ?, ?)""",
            [(run_id, l["skill_type"], l["skill"], l["list_name"], l["facet_count"]) for l in lists],
        )
        self._conn.commit()
        return run_id

    def pending_skill_lists(self, run_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT skill_type, skill, list_name, facet_count FROM skill_run_lists "
            "WHERE run_id = ? AND status != 'done' ORDER BY facet_count DESC, list_name",
            (run_id,),
        ).fetchall()
        return [{"skill_type": r[0], "skill": r[1], "list_name": r[2], "facet_count": r[3]} for r in rows]

    def skill_list_counts(self, run_id: int) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM skill_run_lists WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def save_skill_list_members(
        self, run_id: int, skill_type: str, skill: str, list_name: str, server_total: int,
        members: list[dict[str, Any]],
    ) -> None:
        """Atomic: members + the list's 'done' flag commit together, so a kill
        mid-list leaves it 'pending' with no half-saved members."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM skill_run_members WHERE run_id = ? AND skill_type = ? AND list_name = ?",
                (run_id, skill_type, list_name),
            )
            self._conn.executemany(
                """INSERT OR IGNORE INTO skill_run_members
                   (run_id, skill_type, skill, list_name, account, name, level, class, keypassives_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        run_id, skill_type, skill, list_name, m["account"], m["name"], m["level"], m["class"],
                        json.dumps(list(m["keypassives"]), ensure_ascii=False),
                    )
                    for m in members
                ],
            )
            self._conn.execute(
                "UPDATE skill_run_lists SET status = 'done', server_total = ?, fetched_at = ? "
                "WHERE run_id = ? AND skill_type = ? AND list_name = ?",
                (server_total, _now_iso(), run_id, skill_type, list_name),
            )

    def mark_skill_list_failed(self, run_id: int, skill_type: str, list_name: str) -> None:
        self._conn.execute(
            "UPDATE skill_run_lists SET status = 'failed' WHERE run_id = ? AND skill_type = ? AND list_name = ?",
            (run_id, skill_type, list_name),
        )
        self._conn.commit()

    def iter_run_member_keys(self, run_id: int, excluded_classes: set[str]) -> list[tuple[str, str, int]]:
        """Distinct (account, name, level) across every list of the run,
        excluding cut ascendancy classes. Level is the list row's (what a
        cached detail must match to be reused)."""
        rows = self._conn.execute(
            "SELECT account, name, level, class FROM skill_run_members WHERE run_id = ?", (run_id,)
        ).fetchall()
        seen: dict[tuple[str, str], int] = {}
        for account, name, level, cls in rows:
            if cls in excluded_classes:
                continue
            seen[(account, name)] = level
        return [(a, n, lv) for (a, n), lv in seen.items()]

    def member_stats(self, run_id: int, excluded_classes: set[str]) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT account, name, class FROM skill_run_members WHERE run_id = ?", (run_id,)
        ).fetchall()
        cut_rows = sum(1 for r in rows if r[2] in excluded_classes)
        all_unique = {(r[0], r[1]) for r in rows}
        kept_unique = {(r[0], r[1]) for r in rows if r[2] not in excluded_classes}
        return {
            "member_rows": len(rows), "cut_rows": cut_rows,
            "unique_chars": len(all_unique), "unique_chars_cut": len(all_unique) - len(kept_unique),
        }

    def run_skill_groups(self, run_id: int) -> list[tuple[str, str]]:
        return [
            (r[0], r[1]) for r in self._conn.execute(
                "SELECT DISTINCT skill_type, skill FROM skill_run_members WHERE run_id = ? ORDER BY skill_type, skill",
                (run_id,),
            )
        ]

    def iter_skill_members(self, run_id: int, skill_type: str, skill: str) -> list[tuple[str, str, int, str, list[str]]]:
        """Distinct members of one skill group across its folded lists:
        (account, name, list-row level, class, keypassives)."""
        rows = self._conn.execute(
            "SELECT account, name, level, class, keypassives_json FROM skill_run_members "
            "WHERE run_id = ? AND skill_type = ? AND skill = ?",
            (run_id, skill_type, skill),
        ).fetchall()
        seen: dict[tuple[str, str], tuple[str, str, int, str, list[str]]] = {}
        for account, name, level, cls, kp in rows:
            seen.setdefault((account, name), (account, name, level, cls, json.loads(kp)))
        return list(seen.values())

    def finish_skill_run(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        self._conn.execute(
            "UPDATE skill_runs SET status = ?, finished_at = ?, stats_json = ? WHERE id = ?",
            (status, _now_iso(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        self._conn.commit()

    def update_snapshot_summary(
        self, snapshot_id: int, sample_total: int, unique_characters_fetched: int, fetch_failed: int,
    ) -> None:
        self._conn.execute(
            "UPDATE snapshots SET sample_total = ?, unique_characters_fetched = ?, fetch_failed = ? WHERE id = ?",
            (sample_total, unique_characters_fetched, fetch_failed, snapshot_id),
        )
        self._conn.commit()

    def replace_skill_counts_for_snapshot(
        self, snapshot_id: int, game: str, days_since_league_start: Optional[int], skill_type: str, skill: str,
        stat_rows: list[dict[str, Any]], passive_rows: list[dict[str, Any]], population_rows: list[dict[str, Any]],
    ) -> None:
        """One skill group's results, replaced atomically (idempotent: a
        resumed/recounted group never double-counts)."""
        now = _now_iso()
        with self._conn:
            for table in ("skill_stat_counts", "skill_passive_counts", "skill_populations"):
                self._conn.execute(
                    f"DELETE FROM {table} WHERE snapshot_id = ? AND skill_type = ? AND skill = ?",
                    (snapshot_id, skill_type, skill),
                )
            self._conn.executemany(
                """INSERT INTO skill_stat_counts
                   (snapshot_id, game, skill_type, skill, level_bracket, slot, stat_id, mod_kind, count,
                    population, slot_holders, usage_pct, value_min, value_max, days_since_league_start, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        snapshot_id, game, skill_type, skill, r["level_bracket"], r["slot"], r["stat_id"],
                        r["mod_kind"], r["count"], r["population"], r["slot_holders"], r["usage_pct"],
                        r["value_min"], r["value_max"], days_since_league_start, now,
                    )
                    for r in stat_rows
                ],
            )
            self._conn.executemany(
                """INSERT INTO skill_passive_counts
                   (snapshot_id, game, skill_type, skill, level_bracket, keypassive, count, population,
                    usage_pct, days_since_league_start, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        snapshot_id, game, skill_type, skill, r["level_bracket"], r["keypassive"], r["count"],
                        r["population"], r["usage_pct"], days_since_league_start, now,
                    )
                    for r in passive_rows
                ],
            )
            self._conn.executemany(
                """INSERT INTO skill_populations
                   (snapshot_id, game, skill_type, skill, level_bracket, population, facet_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (snapshot_id, game, skill_type, skill, r["level_bracket"], r["population"], r["facet_count"])
                    for r in population_rows
                ],
            )

    def cleanup_finished_skill_run_checkpoints(self) -> int:
        """Deletes `skill_run_lists`/`skill_run_members` rows for every run
        whose `status = 'finished'` -- once a run is finished it is never
        resumed (`find_resumable_skill_run` only ever looks at 'running'
        rows), so this checkpoint data has zero further use. Called right
        after a run finishes (`skill_collect.run_skill_collection`), not
        deferred to the daily cleanup job, per the project owner's explicit
        "delete immediately" call (2026-09-22) -- these two tables were
        otherwise the single biggest avoidable share of this pipeline's
        per-run disk growth (~13MB/run/game measured live, purely from data
        that already has no purpose). Running/failed/abandoned runs' rows are
        untouched -- resuming or debugging them still needs them. Global (not
        scoped to one run_id) and idempotent, so it's also a safety net for
        any run whose own post-finish cleanup was somehow skipped."""
        with self._conn:
            cur1 = self._conn.execute(
                "DELETE FROM skill_run_lists WHERE run_id IN (SELECT id FROM skill_runs WHERE status = 'finished')"
            )
            cur2 = self._conn.execute(
                "DELETE FROM skill_run_members WHERE run_id IN (SELECT id FROM skill_runs WHERE status = 'finished')"
            )
        return cur1.rowcount + cur2.rowcount

    def cleanup_old_skill_stat_counts(self, older_than_days: int) -> dict[str, int]:
        """Deletes `skill_stat_counts`/`skill_passive_counts`/`skill_populations`
        rows whose snapshot is older than `older_than_days` -- the skill-
        centric dimension's own retention (much denser per snapshot than the
        legacy `mod_counts`/`skill_mod_counts`/`keypassive_counts` tables,
        which keep every row forever by design and are NOT touched here; see
        CLAUDE.md for the disk-growth math this retention exists to bound).

        Safe with `archive_export.py`'s per-league archive: `build_league_archive`
        freezes/merges against the previously published archive rather than
        blindly recomputing, so an age bucket whose raw rows this deletes
        keeps the totals it already had archived instead of shrinking (see
        that module's docstring) -- this must never be called from anywhere
        that skips a fresh export afterward without going through that path.
        Never touches `snapshots`/`skill_runs` rows themselves (tiny, kept as
        history same as every other snapshot)."""
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - older_than_days * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        deleted: dict[str, int] = {}
        with self._conn:
            for table in ("skill_stat_counts", "skill_passive_counts", "skill_populations"):
                cur = self._conn.execute(
                    f"""DELETE FROM {table} WHERE snapshot_id IN (
                            SELECT id FROM snapshots WHERE collection_method = ? AND created_at < ?
                        )""",
                    (COLLECTION_METHOD_SKILL, cutoff),
                )
                deleted[table] = cur.rowcount
        return deleted

    # ---- skill-centric publish (skill-meta phase 22) ---------------------

    def latest_finished_skill_run(self, game: str) -> Optional[dict[str, Any]]:
        """The newest 'finished' skill_runs row for `game`, joined to its
        snapshot's `days_since_league_start` -- what `publish_skill_meta`
        reads. `None` if no run has ever finished (nothing to publish yet)."""
        row = self._conn.execute(
            """SELECT sr.id, sr.snapshot_id, sr.league, s.days_since_league_start, sr.finished_at
               FROM skill_runs sr JOIN snapshots s ON s.id = sr.snapshot_id
               WHERE sr.game = ? AND sr.status = 'finished' ORDER BY sr.id DESC LIMIT 1""",
            (game,),
        ).fetchone()
        if row is None:
            return None
        return {"run_id": row[0], "snapshot_id": row[1], "league": row[2], "days_since_league_start": row[3], "finished_at": row[4]}

    def get_skill_run_population_total(self, run_id: int) -> int:
        row = self._conn.execute("SELECT population_total FROM skill_runs WHERE id = ?", (run_id,)).fetchone()
        return row[0] if row else 0

    def iter_skill_stat_counts_for_snapshot(self, snapshot_id: int) -> list[tuple]:
        return self._conn.execute(
            """SELECT skill_type, skill, level_bracket, slot, stat_id, mod_kind, count, population,
                      slot_holders, usage_pct, value_min, value_max
               FROM skill_stat_counts WHERE snapshot_id = ?""",
            (snapshot_id,),
        ).fetchall()

    def stream_skill_stat_counts_for_snapshot(self, snapshot_id: int) -> Iterator[tuple]:
        """Same rows as `iter_skill_stat_counts_for_snapshot`, but a lazy cursor: nothing is materialised, and the
        rows come back in id order, i.e. each skill's rows contiguously (see `iter_skill_mods`). Must be consumed
        to the end or closed; do not interleave other writes on this connection while iterating."""
        return self._conn.execute(
            """SELECT skill_type, skill, level_bracket, slot, stat_id, mod_kind, count, population,
                      slot_holders, usage_pct, value_min, value_max
               FROM skill_stat_counts WHERE snapshot_id = ? ORDER BY id""",
            (snapshot_id,),
        )

    def iter_skill_passive_counts_for_snapshot(self, snapshot_id: int) -> list[tuple]:
        return self._conn.execute(
            """SELECT skill_type, skill, level_bracket, keypassive, count, population, usage_pct
               FROM skill_passive_counts WHERE snapshot_id = ?""",
            (snapshot_id,),
        ).fetchall()

    def iter_skill_populations_for_snapshot(self, snapshot_id: int) -> list[tuple]:
        return self._conn.execute(
            """SELECT skill_type, skill, level_bracket, population, facet_count
               FROM skill_populations WHERE snapshot_id = ?""",
            (snapshot_id,),
        ).fetchall()

    def iter_skill_stat_counts_for_league(self, game: str, league: str) -> list[tuple]:
        """Every skill_stat_counts row ever recorded for (game, league) from a
        skill-centric (`skill_top100_v1`) snapshot, joined to
        days_since_league_start -- used by archive_export.py. Explicitly
        filtered by collection_method (a no-op today: the legacy job never
        writes to this table) so a future accidental legacy write can never
        silently blend an unreliable-skill snapshot into this archive."""
        return self._conn.execute(
            """SELECT ssc.skill_type, ssc.skill, ssc.level_bracket, ssc.slot, ssc.stat_id, ssc.mod_kind,
                      ssc.count, ssc.population, ssc.slot_holders, ssc.value_min, ssc.value_max,
                      ssc.snapshot_id, ssc.days_since_league_start
               FROM skill_stat_counts ssc JOIN snapshots s ON s.id = ssc.snapshot_id
               WHERE s.game = ? AND s.league = ? AND s.collection_method = ?""",
            (game, league, COLLECTION_METHOD_SKILL),
        ).fetchall()

    def iter_skill_passive_counts_for_league(self, game: str, league: str) -> list[tuple]:
        return self._conn.execute(
            """SELECT spc.skill_type, spc.skill, spc.level_bracket, spc.keypassive, spc.count,
                      spc.population, spc.snapshot_id, spc.days_since_league_start
               FROM skill_passive_counts spc JOIN snapshots s ON s.id = spc.snapshot_id
               WHERE s.game = ? AND s.league = ? AND s.collection_method = ?""",
            (game, league, COLLECTION_METHOD_SKILL),
        ).fetchall()

    def commit(self) -> None:
        self._conn.commit()

    def cleanup_old_char_items(self, older_than_days: int = 90) -> int:
        """Delete char_items rows older than `older_than_days`. Returns rows deleted."""
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute("DELETE FROM char_items WHERE created_at < ?", (cutoff_iso,))
        self._conn.commit()
        return cur.rowcount
