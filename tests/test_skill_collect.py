"""End-to-end skill-centric collection run with fake poe.ninja endpoints and a
real SQLite DB: planning, list/character checkpoints, resume, reuse, guards."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.ninja_builds import CharacterRef, DictionaryValidationError, PopulationFacets, SnapshotVersion
from src.fetchers.ninja_chars import CharacterDetail, ItemDetail, ItemMods
from src.processing.char_cache import SnapshotProvider, StoreBackedCharacterCache
from src.processing.normalizer import ModNormalizer
from src.processing.skill_collect import CollectConfig, run_skill_collection
from src.storage.local_db import COLLECTION_METHOD_SKILL, LocalDb

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ggg_trade2_stats.json"
BRACKETS = [(28, 65), (65, 75), (75, 90), (90, 100)]
LIFE = "+87 to maximum Life"
LEAGUE = "Forbidden Rites"


class _Killed(BaseException):
    """Simulates the process being killed mid-run (not an Exception, so nothing catches it)."""


class _FakeHttp:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url):
        return self._payload


class FakeChars:
    def __init__(self, levels=None, fail=(), kill_at=None):
        self.calls: list[tuple[str, str]] = []
        self.levels = levels or {}
        self.fail = set(fail)
        self.kill_at = kill_at

    def fetch_character(self, snapshot, account, name):
        self.calls.append((account, name))
        if self.kill_at is not None and len(self.calls) == self.kill_at:
            raise _Killed()
        if account in self.fail:
            raise ValueError("boom")
        item = ItemDetail(slot="ring", base_type="Ring", name="r", frame_type_id="Rare", mods=ItemMods(explicit=[LIFE]))
        return CharacterDetail(
            account=account, name=name, level=self.levels.get(account, 95), char_class="X",
            archetype_hint="never used", items=[item],
        )


class FakeBuilds:
    def __init__(self, facets, lists, fail_lists=()):
        self.facets = facets
        self.lists = lists
        self.fail_lists = set(fail_lists)
        self.facet_calls = 0
        self.list_calls: list[tuple[str, str]] = []

    def fetch_population_facets(self, snapshot):
        self.facet_calls += 1
        return self.facets

    def fetch_skill_list(self, snapshot, list_type, name):
        self.list_calls.append((list_type, name))
        if name in self.fail_lists:
            raise ValueError("list boom")
        return self.lists[(list_type, name)]


def ref(account, level=95, cls="Slayer", kps=()):
    return CharacterRef(name=account.upper(), account=account, level=level, char_class=cls,
                        archetype_hint=None, keypassives=tuple(kps))


def std_builds(**kw):
    facets = PopulationFacets(
        total=100_000, skills={"Fireball": 600, "Ice Nova": 400, "Rare Skill": 50}, spirit_skills=None,
        classes={"Slayer": 99_990, "Witch": 10},
    )
    lists = {
        ("main", "Fireball"): (600, [ref("a", kps=("Dance with Death",)), ref("b"), ref("c", cls="Witch")]),
        ("main", "Ice Nova"): (400, [ref("b"), ref("d", level=96)]),
    }
    return FakeBuilds(facets, lists, **kw)


@pytest.fixture
def normalizer(tmp_path) -> ModNormalizer:
    n = ModNormalizer(
        http_client=_FakeHttp(json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))),
        cache_path=tmp_path / "stats_cache.json", unmapped_log_path=tmp_path / "unmapped.log",
    )
    n.load()
    return n


@pytest.fixture
def db(tmp_path):
    d = LocalDb(tmp_path / "t.sqlite3")
    yield d
    d.close()


def run(db, builds, chars, normalizer, game="poe2", **cfg):
    snaps = SnapshotProvider(lambda: SnapshotVersion("l", LEAGUE, "v1", "l"), refresh_seconds=10**9)
    config = CollectConfig(**cfg)
    cache = StoreBackedCharacterCache(db, chars, snaps, game, LEAGUE, config.reuse_max_age_days, memoize=False)
    return run_skill_collection(
        game=game, league=LEAGUE, days_since_league_start=17, builds_fetcher=builds, snapshots=snaps,
        cache=cache, db=db, normalizer=normalizer, level_brackets=BRACKETS, config=config,
    )


def stat_rows(db, snapshot_id, skill):
    return db._conn.execute(
        "SELECT level_bracket, slot, count, population, slot_holders, usage_pct FROM skill_stat_counts "
        "WHERE snapshot_id = ? AND skill = ? ORDER BY level_bracket, slot", (snapshot_id, skill),
    ).fetchall()


def test_happy_path_counts_each_character_only_into_the_lists_it_came_from(db, normalizer):
    builds, chars = std_builds(), FakeChars()
    stats = run(db, builds, chars, normalizer)

    # "Rare Skill" (50 / 100000 = 0.05%) is under 0.1% -> never requested
    assert builds.list_calls == [("main", "Fireball"), ("main", "Ice Nova")]
    # c is a Witch (0.01% of the league) -> cut; b appears in two lists but is fetched once
    assert sorted(a for a, _n in chars.calls) == ["a", "b", "d"]
    assert stats["ascendancy_cut"]["cut_rows"] == 1 and stats["ascendancy_cut"]["unique_chars_cut"] == 1
    assert stats["ascendancy_cut"]["classes"] == ["Witch"]

    snap = stats["snapshot_id"]
    # Fireball: a, b (c cut) -> base 2; Ice Nova: b, d -> base 2. b counted into BOTH, a only into Fireball
    assert stat_rows(db, snap, "Fireball") == [("90-100", "ring", 2, 2, 2, 100.0)]
    assert stat_rows(db, snap, "Ice Nova") == [("90-100", "ring", 2, 2, 2, 100.0)]
    passives = db._conn.execute(
        "SELECT skill, keypassive, count, population FROM skill_passive_counts WHERE snapshot_id = ?", (snap,)
    ).fetchall()
    assert passives == [("Fireball", "Dance with Death", 1, 2)]
    pops = db._conn.execute(
        "SELECT skill, level_bracket, population, facet_count FROM skill_populations WHERE snapshot_id = ? ORDER BY skill",
        (snap,),
    ).fetchall()
    assert pops == [("Fireball", "90-100", 2, 600), ("Ice Nova", "90-100", 2, 400)]

    row = db._conn.execute("SELECT collection_method, days_since_league_start, sample_total FROM snapshots WHERE id = ?", (snap,)).fetchone()
    assert row == (COLLECTION_METHOD_SKILL, 17, 4)
    assert db._conn.execute("SELECT status FROM skill_runs WHERE id = ?", (stats["run_id"],)).fetchone() == ("finished",)
    assert stats["requests_this_attempt"]["lists"] == 2 and stats["requests_this_attempt"]["characters"] == 3
    assert (stats["characters_fetched_new"], stats["characters_reused"]) == (3, 0)


def test_a_kill_mid_run_resumes_without_refetching_lists_or_finished_characters(db, normalizer):
    builds = std_builds()
    with pytest.raises(_Killed):
        run(db, builds, FakeChars(kill_at=3), normalizer)  # dies fetching the 3rd character
    assert db._conn.execute("SELECT status FROM skill_runs").fetchone() == ("running",)
    assert db._conn.execute("SELECT COUNT(*) FROM char_details").fetchone()[0] == 2  # 2 persisted before the kill

    builds2, chars2 = std_builds(), FakeChars()
    stats = run(db, builds2, chars2, normalizer)

    assert stats["resumed"] is True
    assert builds2.facet_calls == 0 and builds2.list_calls == []  # plan + lists came from the checkpoint
    assert len(chars2.calls) == 1  # only the one character that was never fetched
    assert stats["characters_reused"] == 2 and stats["characters_fetched_new"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0] == 1  # same run, not a new one
    assert stat_rows(db, stats["snapshot_id"], "Fireball") == [("90-100", "ring", 2, 2, 2, 100.0)]  # same result as uninterrupted


def test_a_kill_between_lists_resumes_the_remaining_lists_only(db, normalizer):
    class KillOnSecondList(FakeBuilds):
        def fetch_skill_list(self, snapshot, list_type, name):
            if name == "Ice Nova":
                raise _Killed()
            return super().fetch_skill_list(snapshot, list_type, name)

    base = std_builds()
    with pytest.raises(_Killed):
        run(db, KillOnSecondList(base.facets, base.lists), FakeChars(), normalizer)
    assert db.skill_list_counts(1) == {"done": 1, "pending": 1}

    builds2 = std_builds()
    run(db, builds2, FakeChars(), normalizer)
    assert builds2.list_calls == [("main", "Ice Nova")]  # Fireball's list was already checkpointed


def test_a_finished_run_is_not_resumed_and_the_next_run_reuses_stored_characters(db, normalizer):
    run(db, std_builds(), FakeChars(), normalizer)

    chars2 = FakeChars()
    stats = run(db, std_builds(), chars2, normalizer)
    assert stats["resumed"] is False
    assert chars2.calls == []  # same league, same levels, fresh -> all reused
    assert (stats["characters_fetched_new"], stats["characters_reused"]) == (0, 3)
    assert db._conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0] == 2


def test_a_character_whose_level_moved_is_refetched_others_are_reused(db, normalizer):
    run(db, std_builds(), FakeChars(), normalizer)

    builds = std_builds()
    builds.lists[("main", "Ice Nova")] = (400, [ref("b"), ref("d", level=97)])  # d levelled up 96 -> 97
    chars2 = FakeChars(levels={"d": 97})
    stats = run(db, builds, chars2, normalizer)
    assert chars2.calls == [("d", "D")]
    assert (stats["characters_fetched_new"], stats["characters_reused"]) == (1, 2)


def test_a_detail_ahead_of_a_lagging_index_is_still_reused_when_the_list_row_is_unchanged(db, normalizer):
    """d's list row says 96 but the detail endpoint already says 98 (the index
    lags). Same row next run -> reuse; comparing against the detail's own
    level would refetch d on every run until the index catches up."""
    run(db, std_builds(), FakeChars(levels={"d": 98}), normalizer)
    chars2 = FakeChars(levels={"d": 98})
    stats = run(db, std_builds(), chars2, normalizer)
    assert chars2.calls == [] and stats["characters_reused"] == 3


def test_stored_characters_older_than_the_max_age_are_refetched(db, normalizer):
    run(db, std_builds(), FakeChars(), normalizer)
    db._conn.execute("UPDATE char_details SET fetched_at = '2020-01-01T00:00:00Z' WHERE account = 'a'")
    db._conn.commit()

    chars2 = FakeChars()
    run(db, std_builds(), chars2, normalizer, reuse_max_age_days=7.0)
    assert chars2.calls == [("a", "A")]  # the 7-day refresh catches a gear swap at an unchanged level


def test_too_many_failed_lists_abort_without_counting_and_stay_resumable(db, normalizer):
    builds = std_builds(fail_lists={"Ice Nova"})
    with pytest.raises(RuntimeError, match="lists failed"):
        run(db, builds, FakeChars(), normalizer, max_failed_list_pct=10.0)  # 1 of 2 lists = 50%
    assert db._conn.execute("SELECT COUNT(*) FROM skill_stat_counts").fetchone()[0] == 0
    assert db._conn.execute("SELECT status FROM skill_runs").fetchone() == ("running",)

    builds2 = std_builds()  # the endpoint recovered
    stats = run(db, builds2, FakeChars(), normalizer)
    assert stats["resumed"] is True and builds2.list_calls == [("main", "Ice Nova")]  # only the failed list retried
    assert stat_rows(db, stats["snapshot_id"], "Ice Nova")


def test_a_few_failed_lists_below_the_threshold_do_not_abort(db, normalizer):
    lists = {("main", f"S{i}"): (200, [ref(f"p{i}_{j}") for j in range(3)]) for i in range(30)}
    facets = PopulationFacets(total=100_000, skills={f"S{i}": 200 for i in range(30)}, spirit_skills=None, classes={"Slayer": 100_000})
    stats = run(db, FakeBuilds(facets, lists, fail_lists={"S0"}), FakeChars(), normalizer, max_failed_list_pct=5.0)
    assert stats["lists_failed_this_attempt"] == 1 and stats["skill_groups"] == 29


def test_empty_plan_is_refused_and_creates_nothing(db, normalizer):
    facets = PopulationFacets(total=100_000, skills={"Tiny": 5}, spirit_skills=None, classes={"Slayer": 100_000})
    with pytest.raises(RuntimeError, match="no skill lists"):
        run(db, FakeBuilds(facets, {}), FakeChars(), normalizer)
    assert db._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0] == 0


def test_zero_characters_loaded_is_refused_nothing_is_counted(db, normalizer):
    with pytest.raises(RuntimeError, match="0 of 3 character details loaded"):
        run(db, std_builds(), FakeChars(fail={"a", "b", "d"}), normalizer)
    assert db._conn.execute("SELECT COUNT(*) FROM skill_stat_counts").fetchone()[0] == 0


def test_too_many_failed_characters_abort_but_stay_resumable(db, normalizer):
    with pytest.raises(RuntimeError, match="character fetches failed"):
        run(db, std_builds(), FakeChars(fail={"a"}), normalizer, max_failed_char_pct=10.0)  # 1 of 3 = 33%
    assert db._conn.execute("SELECT status FROM skill_runs").fetchone() == ("running",)
    assert db._conn.execute("SELECT COUNT(*) FROM skill_stat_counts").fetchone()[0] == 0


def test_a_failed_character_below_the_threshold_only_shrinks_that_population(db, normalizer):
    stats = run(db, std_builds(), FakeChars(fail={"a"}), normalizer, max_failed_char_pct=50.0)
    assert stats["characters_failed"] == 1
    assert stat_rows(db, stats["snapshot_id"], "Fireball") == [("90-100", "ring", 1, 1, 1, 100.0)]  # only b left


def test_poe1_vaal_lists_fold_into_the_base_skill_and_dedupe_characters(db, normalizer):
    facets = PopulationFacets(
        total=100_000, skills={"Reap": 700, "Vaal Reap": 300}, spirit_skills=None, classes={"Slayer": 100_000}
    )
    lists = {("main", "Reap"): (700, [ref("a"), ref("b")]), ("main", "Vaal Reap"): (300, [ref("b"), ref("c")])}
    builds, chars = FakeBuilds(facets, lists), FakeChars()
    stats = run(db, builds, chars, normalizer, game="poe1")

    assert sorted(builds.list_calls) == [("main", "Reap"), ("main", "Vaal Reap")]
    assert stats["main_skills"] == 1
    assert sorted(a for a, _n in chars.calls) == ["a", "b", "c"]
    assert stat_rows(db, stats["snapshot_id"], "Reap") == [("90-100", "ring", 3, 3, 3, 100.0)]
    assert db._conn.execute("SELECT COUNT(*) FROM skill_stat_counts WHERE skill = 'Vaal Reap'").fetchone()[0] == 0
    pop = db._conn.execute("SELECT population, facet_count FROM skill_populations").fetchone()
    assert pop == (3, 1000)


def test_main_and_spirit_lists_of_the_same_name_are_kept_apart(db, normalizer):
    facets = PopulationFacets(
        total=100_000, skills={"Herald of Ice": 500}, spirit_skills={"Herald of Ice": 400}, classes={"Slayer": 100_000}
    )
    lists = {("main", "Herald of Ice"): (500, [ref("a"), ref("b")]), ("spirit", "Herald of Ice"): (400, [ref("b"), ref("c"), ref("d")])}
    stats = run(db, FakeBuilds(facets, lists), FakeChars(), normalizer)
    rows = db._conn.execute(
        "SELECT skill_type, population FROM skill_populations WHERE snapshot_id = ? ORDER BY skill_type", (stats["snapshot_id"],)
    ).fetchall()
    assert rows == [("main", 2), ("spirit", 3)]


def test_dictionary_validation_error_aborts_the_run_it_is_not_a_failed_list(db, normalizer):
    class Bad(FakeBuilds):
        def fetch_skill_list(self, snapshot, list_type, name):
            raise DictionaryValidationError("unknown class")

    base = std_builds()
    with pytest.raises(DictionaryValidationError):
        run(db, Bad(base.facets, base.lists), FakeChars(), normalizer)


def test_list_size_caps_the_rows_taken_per_list(db, normalizer):
    facets = PopulationFacets(total=100_000, skills={"Big": 900}, spirit_skills=None, classes={"Slayer": 100_000})
    lists = {("main", "Big"): (900, [ref(f"x{i}") for i in range(10)])}
    stats = run(db, FakeBuilds(facets, lists), FakeChars(), normalizer, list_size=4)
    assert stats["characters_distinct"] == 4


def test_ascendancy_cut_is_reported_every_run_and_thresholds_come_from_config(db, normalizer):
    stats = run(db, std_builds(), FakeChars(), normalizer, min_ascendancy_pct=0.001)  # 0.001% -> Witch (0.01%) is kept
    assert stats["ascendancy_cut"]["cut_rows"] == 0 and stats["ascendancy_cut"]["classes"] == []
    stats2 = run(db, std_builds(), FakeChars(), normalizer, min_ascendancy_pct=0.1)
    assert stats2["ascendancy_cut"]["cut_rows"] == 1


def test_recounting_a_group_replaces_its_rows_never_double_counts(db, normalizer):
    stats = run(db, std_builds(), FakeChars(), normalizer)
    snap = stats["snapshot_id"]
    db.replace_skill_counts_for_snapshot(snap, "poe2", 17, "main", "Fireball", [], [], [])
    assert stat_rows(db, snap, "Fireball") == []
    assert stat_rows(db, snap, "Ice Nova")  # other groups untouched


def test_collect_config_reads_the_yaml_section_and_falls_back_to_defaults():
    cfg = {"skill_meta": {"collect": {"min_skill_pct": 0.5, "fold_vaal": False, "list_size": 50}}}
    c = CollectConfig.from_cfg(cfg)
    assert (c.min_skill_pct, c.fold_vaal, c.list_size, c.reuse_max_age_days) == (0.5, False, 50, 7.0)
    assert CollectConfig.from_cfg({}) == CollectConfig()
