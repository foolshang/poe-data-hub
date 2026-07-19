from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.ninja_builds import CharacterRef
from src.fetchers.ninja_chars import CharacterDetail, ItemDetail, ItemMods
from src.processing.aggregator import aggregate_jewels, aggregate_slot, compute_archetype_distribution
from src.processing.archetyper import Archetyper
from src.processing.normalizer import ModNormalizer

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ggg_trade2_stats.json"


class _FakeHttpClient:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url):
        return self._payload


@pytest.fixture
def normalizer(tmp_path) -> ModNormalizer:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    n = ModNormalizer(
        http_client=_FakeHttpClient(payload),
        cache_path=tmp_path / "stats_cache.json",
        unmapped_log_path=tmp_path / "unmapped.log",
    )
    n.load()
    return n


@pytest.fixture
def archetyper() -> Archetyper:
    return Archetyper()


def _ring(mods_explicit):
    return ItemDetail(slot="ring", base_type="Ring", name="x", frame_type_id="Rare", mods=ItemMods(explicit=mods_explicit))


def _weapon(base_type):
    return ItemDetail(slot="weapon", base_type=base_type, name="w", frame_type_id="Rare", mods=ItemMods())


def _jewel(base_slot, mods_explicit):
    return ItemDetail(slot=base_slot, base_type="Jewel", name="j", frame_type_id="Rare", mods=ItemMods(explicit=mods_explicit))


def test_aggregate_slot_counts_per_character_not_per_item(normalizer, archetyper):
    char_a = CharacterDetail(
        account="a", name="A", level=100, char_class="X", main_skill="Herald of Ash",
        items=[_ring(["+87 to maximum Life"]), _weapon("Wand")],
    )
    char_b = CharacterDetail(
        account="b", name="B", level=100, char_class="X", main_skill="Shield Wall",
        items=[_ring(["+87 to maximum Life"]), _ring(["+23% to Cold Resistance"]), _weapon("Two Handed Mace")],
    )
    lookup = {("a", "A"): char_a, ("b", "B"): char_b}
    refs = [CharacterRef(name="A", account="a", level=100, char_class="X", main_skill="Herald of Ash"),
            CharacterRef(name="B", account="b", level=100, char_class="X", main_skill="Shield Wall")]

    results = aggregate_slot("ring", refs, lambda acc, nm: lookup[(acc, nm)], archetyper, normalizer)
    by_key = {(r.archetype, r.stat_id): r for r in results}

    life_id = "explicit.stat_3299347043"
    cold_id = "explicit.stat_4220027924"

    assert by_key[("all", life_id)].count == 2
    assert by_key[("all", life_id)].usage_pct == 100.0
    assert by_key[("all", cold_id)].count == 1
    assert by_key[("all", cold_id)].usage_pct == 50.0

    assert by_key[("spell-fire", life_id)].count == 1
    assert by_key[("attack-melee", life_id)].count == 1
    assert ("spell-fire", cold_id) not in by_key


def test_aggregate_slot_value_min_max(normalizer, archetyper):
    char_a = CharacterDetail(account="a", name="A", level=100, char_class="X", main_skill=None,
                              items=[_ring(["+40 to maximum Life"])])
    char_b = CharacterDetail(account="b", name="B", level=100, char_class="X", main_skill=None,
                              items=[_ring(["+115 to maximum Life"])])
    lookup = {("a", "A"): char_a, ("b", "B"): char_b}
    refs = [CharacterRef(name="A", account="a", level=100, char_class="X", main_skill=None),
            CharacterRef(name="B", account="b", level=100, char_class="X", main_skill=None)]

    results = aggregate_slot("ring", refs, lambda acc, nm: lookup[(acc, nm)], archetyper, normalizer)
    life = next(r for r in results if r.archetype == "all")
    assert life.value_min == 40
    assert life.value_max == 115


def test_aggregate_slot_empty_list_returns_empty(normalizer, archetyper):
    assert aggregate_slot("ring", [], lambda a, n: None, archetyper, normalizer) == []


def test_aggregate_slot_skips_missing_character_detail(normalizer, archetyper):
    refs = [CharacterRef(name="A", account="a", level=100, char_class="X", main_skill=None)]
    results = aggregate_slot("ring", refs, lambda a, n: None, archetyper, normalizer)
    assert results == []


def test_aggregate_jewels_denominator_is_per_gem(normalizer, archetyper):
    # char A has TWO emerald jewels (both with the same mod) -> denominator counts 2 gems, not 1 character.
    char_a = CharacterDetail(
        account="a", name="A", level=100, char_class="X", main_skill=None,
        items=[_jewel("jewel:emerald", ["+23% to Cold Resistance"]), _jewel("jewel:emerald", ["+23% to Cold Resistance"])],
    )
    lookup = {("a", "A"): char_a}
    refs = [CharacterRef(name="A", account="a", level=100, char_class="X", main_skill=None)]

    results, denom_by_base = aggregate_jewels(refs, lambda acc, nm: lookup[(acc, nm)], archetyper, normalizer)
    all_result = next(r for r in results if r.archetype == "all")
    assert all_result.slot == "jewel:emerald"
    assert all_result.count == 2
    assert all_result.usage_pct == 100.0  # 2 hits / 2 gems
    assert denom_by_base == {"jewel:emerald": 2}


def test_aggregate_jewels_different_base_types_are_separate(normalizer, archetyper):
    char_a = CharacterDetail(
        account="a", name="A", level=100, char_class="X", main_skill=None,
        items=[_jewel("jewel:emerald", ["+23% to Cold Resistance"]), _jewel("jewel:sapphire", ["+23% to Cold Resistance"])],
    )
    lookup = {("a", "A"): char_a}
    refs = [CharacterRef(name="A", account="a", level=100, char_class="X", main_skill=None)]

    results, denom_by_base = aggregate_jewels(refs, lambda acc, nm: lookup[(acc, nm)], archetyper, normalizer)
    slots = {r.slot for r in results}
    assert slots == {"jewel:emerald", "jewel:sapphire"}
    assert denom_by_base == {"jewel:emerald": 1, "jewel:sapphire": 1}
    for r in results:
        if r.archetype == "all":
            assert r.count == 1
            assert r.usage_pct == 100.0


def test_compute_archetype_distribution(normalizer, archetyper):
    char_a = CharacterDetail(account="a", name="A", level=100, char_class="X", main_skill="Herald of Ash", items=[])
    char_b = CharacterDetail(account="b", name="B", level=100, char_class="X", main_skill="Herald of Ice", items=[])
    char_c = CharacterDetail(account="c", name="C", level=100, char_class="X", main_skill="Shield Wall", items=[_weapon("Bow")])
    lookup = {("a", "A"): char_a, ("b", "B"): char_b, ("c", "C"): char_c}
    refs = [CharacterRef(name="A", account="a", level=100, char_class="X", main_skill="Herald of Ash"),
            CharacterRef(name="B", account="b", level=100, char_class="X", main_skill="Herald of Ice"),
            CharacterRef(name="C", account="c", level=100, char_class="X", main_skill="Shield Wall")]

    dist = compute_archetype_distribution(refs, lambda acc, nm: lookup[(acc, nm)], archetyper)
    by_archetype = {d["archetype"]: d["pct"] for d in dist}
    assert by_archetype["spell-fire"] == pytest.approx(33.33, rel=0.01)
    assert by_archetype["spell-cold"] == pytest.approx(33.33, rel=0.01)
    assert by_archetype["attack-bow"] == pytest.approx(33.33, rel=0.01)


def test_compute_archetype_distribution_empty_refs_returns_empty_list(archetyper):
    """A brand-new league's first few hours can have zero characters
    indexed yet (poe.ninja hasn't caught up) -- this must never
    divide-by-zero or otherwise raise; the meta job should just publish an
    empty-but-valid snapshot for that cycle, not crash (spec 9,
    league_start_mode's automatic higher-frequency polling makes this more
    likely to actually be hit than under the old once-daily cadence)."""
    assert compute_archetype_distribution([], lambda acc, nm: None, archetyper) == []


def test_rank_in_slot_orders_by_count_desc(normalizer, archetyper):
    char_a = CharacterDetail(account="a", name="A", level=100, char_class="X", main_skill=None,
                              items=[_ring(["+87 to maximum Life", "+23% to Cold Resistance"])])
    char_b = CharacterDetail(account="b", name="B", level=100, char_class="X", main_skill=None,
                              items=[_ring(["+87 to maximum Life"])])
    lookup = {("a", "A"): char_a, ("b", "B"): char_b}
    refs = [CharacterRef(name="A", account="a", level=100, char_class="X", main_skill=None),
            CharacterRef(name="B", account="b", level=100, char_class="X", main_skill=None)]

    results = aggregate_slot("ring", refs, lambda acc, nm: lookup[(acc, nm)], archetyper, normalizer)
    all_results = sorted((r for r in results if r.archetype == "all"), key=lambda r: r.rank_in_slot)
    assert all_results[0].count == 2
    assert all_results[0].rank_in_slot == 1
    assert all_results[1].count == 1
    assert all_results[1].rank_in_slot == 2
