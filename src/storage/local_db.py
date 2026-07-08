"""SQLite storage for meta snapshots (spec 6.5).

Three tables:
  snapshots   -- one row per meta job run. Kept forever.
  char_items  -- one row per fetched character, raw item JSON attached.
                 Deleted after 90 days (cleanup_old_char_items).
  mod_counts  -- aggregated mod frequency per (slot, archetype, stat_id).
                 Kept forever across leagues on purpose: meta shifts with
                 league age (early-league = res/life, late-league = damage),
                 so keeping mod_counts across leagues lets next league compare
                 against "last league's early days" (spec 6.5).
"""
from __future__ import annotations

import json
import sqlite3
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
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LocalDb:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert_snapshot(
        self,
        league: str,
        days_since_league_start: Optional[int],
        sample_total: int,
        unique_characters_fetched: int,
        fetch_failed: int,
        snapshot_date: Optional[str] = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO snapshots
               (snapshot_date, league, days_since_league_start, sample_total,
                unique_characters_fetched, fetch_failed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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
    ) -> None:
        self._conn.execute(
            """INSERT INTO char_items
               (snapshot_id, account, name, level, class, archetype, items_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, account, name, level, char_class, archetype, json.dumps(items_raw, ensure_ascii=False), _now_iso()),
        )

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
