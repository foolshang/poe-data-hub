"""Shared SQLite-backed character cache: reuse rules, failure accounting, and
snapshot-version handling."""
from __future__ import annotations

import pytest

from src.fetchers.ninja_builds import SnapshotVersion
from src.fetchers.ninja_chars import CharacterDetail, ItemDetail, ItemMods
from src.processing.char_cache import SnapshotProvider, StoreBackedCharacterCache
from src.storage.local_db import LocalDb


class FakeChars:
    def __init__(self, level=95, bad_versions=()):
        self.calls: list[tuple[str, str, str]] = []
        self.level = level
        self.bad_versions = set(bad_versions)

    def fetch_character(self, snapshot, account, name):
        self.calls.append((snapshot.version, account, name))
        if snapshot.version in self.bad_versions:
            raise ValueError(f"404 for version {snapshot.version}")
        item = ItemDetail(slot="ring", base_type="Ring", name="r", frame_type_id="Rare", mods=ItemMods(explicit=["x"]))
        return CharacterDetail(account=account, name=name, level=self.level, char_class="Slayer",
                               archetype_hint="Fireball", items=[item], passive_selection=(1, 2))


@pytest.fixture
def db(tmp_path):
    d = LocalDb(tmp_path / "t.sqlite3")
    yield d
    d.close()


def make(db, chars, league="L", max_age=7.0, memoize=False, versions=None):
    seq = iter(versions or ["v1"] * 50)
    snaps = SnapshotProvider(lambda: SnapshotVersion("l", "L", next(seq), "l"), refresh_seconds=10**9)
    return StoreBackedCharacterCache(db, chars, snaps, "poe1", league, max_age, memoize=memoize), snaps


def test_first_get_fetches_and_persists_second_get_reuses(db):
    chars = FakeChars()
    cache, _ = make(db, chars)
    d1 = cache.get("acc", "Name", 95)
    d2 = cache.get("acc", "Name", 95)

    assert len(chars.calls) == 1
    assert (cache.fetched_count, cache.reused_count) == (1, 1)
    assert d1 == d2 and d2.passive_selection == (1, 2) and d2.archetype_hint == "Fireball"
    assert db.get_char_detail_any("poe1", "acc", "Name")["level"] == 95


def test_reuse_survives_a_new_cache_object_that_is_the_point_of_persisting(db):
    chars = FakeChars()
    make(db, chars)[0].get("acc", "Name", 95)
    cache2, _ = make(db, chars)
    assert cache2.get("acc", "Name", 95) is not None
    assert len(chars.calls) == 1 and cache2.reused_count == 1


def test_a_different_list_row_level_forces_a_refetch(db):
    chars = FakeChars(level=96)
    cache, _ = make(db, chars)
    cache.get("acc", "Name", 95)
    cache.get("acc", "Name", 96)  # the list row moved on
    assert len(chars.calls) == 2


def test_a_different_league_forces_a_refetch(db):
    chars = FakeChars()
    make(db, chars, league="Old League")[0].get("acc", "Name", 95)
    make(db, chars, league="New League")[0].get("acc", "Name", 95)
    assert len(chars.calls) == 2


def test_an_entry_older_than_max_age_is_refetched(db):
    chars = FakeChars()
    cache, _ = make(db, chars, max_age=7.0)
    cache.get("acc", "Name", 95)
    db._conn.execute("UPDATE char_details SET fetched_at = '2020-01-01T00:00:00Z'")
    db._conn.commit()
    make(db, chars, max_age=7.0)[0].get("acc", "Name", 95)
    assert len(chars.calls) == 2


def test_no_level_means_always_fetch_never_reuse(db):
    chars = FakeChars()
    cache, _ = make(db, chars)
    cache.get("acc", "Name", None)
    make(db, chars)[0].get("acc", "Name", None)
    assert len(chars.calls) == 2


def test_failures_are_counted_returned_as_none_and_never_stored(db):
    chars = FakeChars(bad_versions={"v1"})
    cache, _ = make(db, chars)
    assert cache.get("acc", "Name", 95) is None
    assert (cache.failed_count, cache.fetched_count) == (1, 0)
    assert db.get_char_detail_any("poe1", "acc", "Name") is None


def test_memoize_keeps_results_in_memory_for_the_run(db):
    chars = FakeChars()
    cache, _ = make(db, chars, memoize=True)
    cache.get("acc", "Name", 95)
    db._conn.execute("DELETE FROM char_details")  # gone from the DB, still served from memory
    assert cache.get("acc", "Name", 95) is not None
    assert cache.cache_hit_count == 1 and cache.unique_ok_count == 1


def test_non_memoizing_cache_holds_nothing_in_memory(db):
    cache, _ = make(db, FakeChars(), memoize=False)
    cache.get("acc", "Name", 95)
    assert cache._memo is None


def test_get_stored_reads_whatever_is_in_the_db_without_any_network(db):
    chars = FakeChars()
    cache, _ = make(db, chars)
    cache.get("acc", "Name", 95)
    db._conn.execute("UPDATE char_details SET league = 'Other', fetched_at = '2020-01-01T00:00:00Z'")
    db._conn.commit()
    calls_before = len(chars.calls)
    assert cache.get_stored("acc", "Name") is not None  # no freshness/league check by design
    assert cache.get_stored("acc", "Ghost") is None
    assert len(chars.calls) == calls_before


def test_a_failed_fetch_retries_once_with_a_re_resolved_snapshot(db, monkeypatch):
    """The index version can roll over mid-run; a failure against a stale
    version is retried against a freshly resolved one."""
    chars = FakeChars(bad_versions={"v1"})
    cache, snaps = make(db, chars, versions=["v1", "v2"])
    monkeypatch.setattr(snaps, "age_seconds", lambda: 3600.0)
    assert cache.get("acc", "Name", 95) is not None
    assert [c[0] for c in chars.calls] == ["v1", "v2"]
    assert snaps.resolve_count == 2


def test_a_freshly_resolved_snapshot_is_not_re_resolved_on_failure(db):
    chars = FakeChars(bad_versions={"v1"})
    cache, snaps = make(db, chars, versions=["v1", "v2"])
    assert cache.get("acc", "Name", 95) is None  # snapshot is seconds old: a real failure, no retry
    assert len(chars.calls) == 1 and snaps.resolve_count == 1


def test_snapshot_provider_refreshes_on_its_timer_and_survives_a_failed_refresh():
    versions = iter(["v1", "v2", RuntimeError("index-state down"), "v4"])

    def resolve():
        v = next(versions)
        if isinstance(v, Exception):
            raise v
        return SnapshotVersion("l", "L", v, "l")

    snaps = SnapshotProvider(resolve, refresh_seconds=0)  # every get() is due
    assert snaps.get().version == "v2"
    assert snaps.get().version == "v2"  # refresh failed -> keeps the last good one
    assert snaps.get().version == "v4"
