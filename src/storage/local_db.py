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
from typing import Any, Optional

DEFAULT_DB_PATH = Path("data/poe_data_hub.sqlite3")

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
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LocalDb:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
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
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO snapshots
               (snapshot_date, game, league, days_since_league_start, sample_total,
                unique_characters_fetched, fetch_failed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                game,
                league,
                days_since_league_start,
                sample_total,
                unique_characters_fetched,
                fetch_failed,
                _now_iso(),
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
    ) -> None:
        self._conn.execute(
            """INSERT INTO char_items
               (snapshot_id, account, name, level, class, archetype, items_json, main_skill, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id, account, name, level, char_class, archetype,
                json.dumps(items_raw, ensure_ascii=False), main_skill, _now_iso(),
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
        return self._conn.execute(
            """SELECT smc.slot, smc.main_skill, smc.level_bracket, smc.archetype, smc.stat_id, smc.mod_kind,
                      smc.count, smc.population, smc.value_min, smc.value_max, smc.snapshot_id,
                      smc.days_since_league_start
               FROM skill_mod_counts smc JOIN snapshots s ON s.id = smc.snapshot_id
               WHERE s.game = ? AND s.league = ?""",
            (game, league),
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

    def commit(self) -> None:
        self._conn.commit()

    def cleanup_old_char_items(self, older_than_days: int = 90) -> int:
        """Delete char_items rows older than `older_than_days`. Returns rows deleted."""
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = self._conn.execute("DELETE FROM char_items WHERE created_at < ?", (cutoff_iso,))
        self._conn.commit()
        return cur.rowcount
