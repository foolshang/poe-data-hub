"""SQLite-backed character-detail cache shared by both collection jobs.

Replaces the old in-memory-only per-run cache: a fetched character
is persisted to `char_details` the moment it arrives (so a killed run loses
nothing), and reused on later runs while it is still trustworthy -- same
league, same list-row level as when it was fetched, and younger than
`reuse_max_age_days` (a level-capped character can still swap gear, so the
age cap forces a periodic refetch).

poe.ninja's index `version` (part of every character URL) rolls over during
a long run, so the snapshot is re-resolved every `snapshot_refresh_seconds`
and once more if a fetch fails, instead of trusting the version captured when
the run started.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from src.fetchers.ninja_builds import SnapshotVersion
from src.fetchers.ninja_chars import (
    CharacterDetail, NinjaCharsFetcher, character_detail_from_dict, character_detail_to_dict,
)
from src.storage.local_db import LocalDb

log = logging.getLogger(__name__)


class SnapshotProvider:
    """Hands out the current poe.ninja snapshot, re-resolving it on a timer."""

    def __init__(self, resolve: Callable[[], SnapshotVersion], refresh_seconds: float = 1800.0):
        self._resolve = resolve
        self._refresh_seconds = refresh_seconds
        self._snapshot = resolve()
        self._resolved_at = time.monotonic()
        self.resolve_count = 1

    def get(self) -> SnapshotVersion:
        if time.monotonic() - self._resolved_at >= self._refresh_seconds:
            self.refresh()
        return self._snapshot

    def refresh(self) -> SnapshotVersion:
        try:
            fresh = self._resolve()
        except Exception as e:  # keep working with the last known snapshot
            log.warning("snapshot refresh failed, keeping %s: %s", self._snapshot.version, e)
            self._resolved_at = time.monotonic()
            return self._snapshot
        if fresh.version != self._snapshot.version:
            log.info("poe.ninja snapshot version changed: %s -> %s", self._snapshot.version, fresh.version)
        self._snapshot = fresh
        self._resolved_at = time.monotonic()
        self.resolve_count += 1
        return fresh

    def age_seconds(self) -> float:
        return time.monotonic() - self._resolved_at


class StoreBackedCharacterCache:
    """`get(account, name, level)` -> CharacterDetail or None.

    `level` is the level poe.ninja's list row showed; passing it enables
    reuse (`None` skips reuse and always fetches). The row level is stored
    next to the detail and compared against the *next* list row's level --
    not against the detail's own level, which can be ahead of a lagging
    index. `memoize=True` also keeps every result in memory for the run (the
    legacy per-slot job re-reads the same character across many slots); the
    skill-centric job passes False so it never holds the population in RAM.
    """

    def __init__(
        self, db: LocalDb, fetcher: NinjaCharsFetcher, snapshots: SnapshotProvider,
        game: str, league: str, max_age_days: float, memoize: bool = False,
    ):
        self._db = db
        self._fetcher = fetcher
        self._snapshots = snapshots
        self._game = game
        self._league = league
        self._max_age_days = max_age_days
        self._memo: Optional[dict[tuple[str, str], Optional[CharacterDetail]]] = {} if memoize else None
        self._ok_keys: set[tuple[str, str]] = set()
        self.fetched_count = 0
        self.reused_count = 0
        self.failed_count = 0
        self.cache_hit_count = 0

    @property
    def unique_ok_count(self) -> int:
        """Distinct characters that yielded a detail this run (fetched or reused)."""
        return len(self._ok_keys)

    def get(self, account: str, name: str, level: Optional[int] = None) -> Optional[CharacterDetail]:
        key = (account, name)
        if self._memo is not None and key in self._memo:
            self.cache_hit_count += 1
            return self._memo[key]

        detail = self._load_or_fetch(account, name, level)
        if detail is not None:
            self._ok_keys.add(key)
        if self._memo is not None:
            self._memo[key] = detail
        return detail

    def get_stored(self, account: str, name: str) -> Optional[CharacterDetail]:
        """Whatever detail is stored, no freshness check and no network --
        used by the counting phase after every fetch has been attempted."""
        raw = self._db.get_char_detail_any(self._game, account, name)
        return character_detail_from_dict(raw) if raw is not None else None

    def _load_or_fetch(self, account: str, name: str, level: Optional[int]) -> Optional[CharacterDetail]:
        if level is not None:
            stored = self._db.get_fresh_char_detail(self._game, account, name, self._league, level, self._max_age_days)
            if stored is not None:
                self.reused_count += 1
                return character_detail_from_dict(stored)
        try:
            detail = self._fetch(account, name)
        except Exception as e:
            log.warning("ninja_chars: skip character %s/%s: %s", account, name, e)
            self.failed_count += 1
            return None
        self.fetched_count += 1
        self._db.upsert_char_detail(
            self._game, self._league, character_detail_to_dict(detail),
            list_level=level if level is not None else detail.level,
        )
        return detail

    def _fetch(self, account: str, name: str) -> CharacterDetail:
        try:
            return self._fetcher.fetch_character(self._snapshots.get(), account, name)
        except Exception:
            # The index version may have rolled over since it was resolved;
            # one retry against a freshly resolved snapshot before giving up.
            if self._snapshots.age_seconds() < 60:
                raise
            return self._fetcher.fetch_character(self._snapshots.refresh(), account, name)
