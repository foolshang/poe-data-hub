from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.ninja_builds import CharacterRef
from src.fetchers.ninja_chars import CharacterDetail, ItemDetail, ItemMods
from src.processing.aggregator import (
    aggregate_jewels,
    aggregate_jewels_by_skill,
    aggregate_keypassives_by_skill,
    aggregate_slot,
    aggregate_slot_by_skill,
    bracket_for_level,
    compute_archetype_distribution,
    compute_skill_bracket_population,
    reprocess_chars_to_skill_mod_counts,
)
from src.processing.archetyper import Archetyper
from src.processing.normalizer import ModNormalizer
from src.processing.skill_normalizer import SkillNormalizer

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


class _FakeGemsHttp:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url):
        return self._payload


@pytest.fixture
def skill_normalizer() -> SkillNormalizer:
    payload = {
        "gems": [
            {"id": "Fireball", "display_name": "Fireball", "is_support": False},
            {"id": "IceNovaAltX", "display_name": "Ice Nova of Frostbolts", "is_support": False},
        ]
    }
    n = SkillNormalizer(_FakeGemsHttp(payload), game="poe2", gems_url="https://x/poe2/gems.json")
    n.load()
    return n


LEVEL_BRACKETS = [(28, 65), (65, 75), (75, 90), (90, 100)]


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


# ---------------------------------------------------------------------------
# Skill-meta phase 2: bracket_for_level, aggregate_*_by_skill,
# compute_skill_bracket_population, reprocess_chars_to_skill_mod_counts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level,expected", [
    (1, None),          # below the first bracket's lo -- excluded from this dimension
    (27, None),
    (28, "28-65"),
    (64, "28-65"),
    (65, "65-75"),       # boundary: half-open, belongs to the bracket it's the lo of
    (74, "65-75"),
    (75, "75-90"),
    (89, "75-90"),
    (90, "90-100"),
    (99, "90-100"),
    (100, "90-100"),     # last bracket is closed so level 100 lands somewhere
])
def test_bracket_for_level(level, expected):
    assert bracket_for_level(level, LEVEL_BRACKETS) == expected


def test_bracket_for_level_none_level_returns_none():
    assert bracket_for_level(None, LEVEL_BRACKETS) is None


def test_aggregate_slot_by_skill_normalizes_and_buckets_by_bracket(normalizer, archetyper, skill_normalizer):
    char_a = CharacterDetail(account="a", name="A", level=95, char_class="X", main_skill="Fireball",
                              items=[_ring(["+87 to maximum Life"])])
    char_b = CharacterDetail(account="b", name="B", level=30, char_class="X", main_skill="Fireball",
                              items=[_ring(["+87 to maximum Life"])])
    char_c = CharacterDetail(account="c", name="C", level=95, char_class="X", main_skill="Some Unknown Gem",
                              items=[_ring(["+87 to maximum Life"])])
    lookup = {("a", "A"): char_a, ("b", "B"): char_b, ("c", "C"): char_c}
    refs = [CharacterRef(name="A", account="a", level=95, char_class="X", main_skill="Fireball"),
            CharacterRef(name="B", account="b", level=30, char_class="X", main_skill="Fireball"),
            CharacterRef(name="C", account="c", level=95, char_class="X", main_skill="Some Unknown Gem")]

    results = aggregate_slot_by_skill("ring", refs, lambda acc, nm: lookup[(acc, nm)], normalizer, skill_normalizer, archetyper, LEVEL_BRACKETS)
    by_key = {(r.main_skill, r.level_bracket): r for r in results}

    assert by_key[("Fireball", "90-100")].population == 1
    assert by_key[("Fireball", "28-65")].population == 1
    assert by_key[("unknown", "90-100")].population == 1
    assert ("Fireball", "90-100") != ("Fireball", "28-65")  # distinct brackets, not merged


def test_aggregate_slot_by_skill_excludes_below_bracket_range(normalizer, archetyper, skill_normalizer):
    char_a = CharacterDetail(account="a", name="A", level=5, char_class="X", main_skill="Fireball",
                              items=[_ring(["+87 to maximum Life"])])
    lookup = {("a", "A"): char_a}
    refs = [CharacterRef(name="A", account="a", level=5, char_class="X", main_skill="Fireball")]
    results = aggregate_slot_by_skill("ring", refs, lambda acc, nm: lookup[(acc, nm)], normalizer, skill_normalizer, archetyper, LEVEL_BRACKETS)
    assert results == []


def test_aggregate_jewels_by_skill_counts_per_gem_instance(normalizer, archetyper, skill_normalizer):
    char_a = CharacterDetail(
        account="a", name="A", level=95, char_class="X", main_skill="Fireball",
        items=[_jewel("jewel:emerald", ["+23% to Cold Resistance"]), _jewel("jewel:emerald", ["+23% to Cold Resistance"])],
    )
    lookup = {("a", "A"): char_a}
    refs = [CharacterRef(name="A", account="a", level=95, char_class="X", main_skill="Fireball")]

    results = aggregate_jewels_by_skill(refs, lambda acc, nm: lookup[(acc, nm)], normalizer, skill_normalizer, archetyper, LEVEL_BRACKETS)
    row = next(r for r in results if r.slot == "jewel:emerald")
    assert row.main_skill == "Fireball"
    assert row.level_bracket == "90-100"
    assert row.population == 2  # two gem instances, not one character
    assert row.count == 2


def test_compute_skill_bracket_population(normalizer, archetyper, skill_normalizer):
    char_a = CharacterDetail(account="a", name="A", level=95, char_class="X", main_skill="Fireball", items=[])
    char_b = CharacterDetail(account="b", name="B", level=96, char_class="X", main_skill="Fireball", items=[])
    char_c = CharacterDetail(account="c", name="C", level=30, char_class="X", main_skill="Fireball", items=[])
    lookup = {("a", "A"): char_a, ("b", "B"): char_b, ("c", "C"): char_c}
    refs = [CharacterRef(name="A", account="a", level=95, char_class="X", main_skill="Fireball"),
            CharacterRef(name="B", account="b", level=96, char_class="X", main_skill="Fireball"),
            CharacterRef(name="C", account="c", level=30, char_class="X", main_skill="Fireball")]

    pop = compute_skill_bracket_population(refs, lambda acc, nm: lookup[(acc, nm)], skill_normalizer, LEVEL_BRACKETS)
    assert pop == {("Fireball", "90-100"): 2, ("Fireball", "28-65"): 1}


def test_aggregate_keypassives_by_skill_counts_per_character(normalizer, archetyper, skill_normalizer):
    char_a = CharacterDetail(account="a", name="A", level=95, char_class="X", main_skill="Fireball", items=[])
    char_b = CharacterDetail(account="b", name="B", level=96, char_class="X", main_skill="Fireball", items=[])
    refs = [
        CharacterRef(name="A", account="a", level=95, char_class="X", main_skill="Fireball", keypassives=("Dance with Death",)),
        CharacterRef(name="B", account="b", level=96, char_class="X", main_skill="Fireball", keypassives=("Dance with Death", "Ancestral Bond")),
    ]
    lookup = {("a", "A"): char_a, ("b", "B"): char_b}

    results = aggregate_keypassives_by_skill(refs, lambda acc, nm: lookup[(acc, nm)], skill_normalizer, archetyper, LEVEL_BRACKETS)

    by_kp = {r.keypassive: r for r in results}
    assert by_kp["Dance with Death"].count == 2
    assert by_kp["Dance with Death"].population == 2
    assert by_kp["Dance with Death"].usage_pct == 100.0
    assert by_kp["Ancestral Bond"].count == 1
    assert by_kp["Ancestral Bond"].usage_pct == 50.0
    assert all(r.main_skill == "Fireball" and r.level_bracket == "90-100" for r in results)


def test_aggregate_keypassives_by_skill_dedupes_repeated_keypassive_per_character(normalizer, archetyper, skill_normalizer):
    """A character listing the same keypassive twice (shouldn't happen, but
    defensively) must count once, same rule as a stat_id on regular slots."""
    char_a = CharacterDetail(account="a", name="A", level=95, char_class="X", main_skill="Fireball", items=[])
    refs = [CharacterRef(name="A", account="a", level=95, char_class="X", main_skill="Fireball", keypassives=("Dance with Death", "Dance with Death"))]
    lookup = {("a", "A"): char_a}

    results = aggregate_keypassives_by_skill(refs, lambda acc, nm: lookup[(acc, nm)], skill_normalizer, archetyper, LEVEL_BRACKETS)

    assert len(results) == 1
    assert results[0].count == 1


def test_aggregate_keypassives_by_skill_skips_characters_below_first_bracket(normalizer, archetyper, skill_normalizer):
    char_a = CharacterDetail(account="a", name="A", level=10, char_class="X", main_skill="Fireball", items=[])
    refs = [CharacterRef(name="A", account="a", level=10, char_class="X", main_skill="Fireball", keypassives=("Dance with Death",))]
    lookup = {("a", "A"): char_a}

    results = aggregate_keypassives_by_skill(refs, lambda acc, nm: lookup[(acc, nm)], skill_normalizer, archetyper, LEVEL_BRACKETS)

    assert results == []


def test_aggregate_keypassives_by_skill_no_keypassives_produces_no_rows(normalizer, archetyper, skill_normalizer):
    char_a = CharacterDetail(account="a", name="A", level=95, char_class="X", main_skill="Fireball", items=[])
    refs = [CharacterRef(name="A", account="a", level=95, char_class="X", main_skill="Fireball")]  # keypassives defaults to ()
    lookup = {("a", "A"): char_a}

    results = aggregate_keypassives_by_skill(refs, lambda acc, nm: lookup[(acc, nm)], skill_normalizer, archetyper, LEVEL_BRACKETS)

    assert results == []


def test_reprocess_chars_to_skill_mod_counts_matches_live_aggregation(normalizer, archetyper, skill_normalizer):
    """The offline reprocess path (item 13, no CharacterRef/get_detail) must
    produce the same population/count as the live aggregate_slot_by_skill
    path for equivalent input."""
    items = [_ring(["+87 to maximum Life"])]
    chars = [("Fireball", 95, items)]

    live_char = CharacterDetail(account="a", name="A", level=95, char_class="X", main_skill="Fireball", items=items)
    live_results = aggregate_slot_by_skill(
        "ring", [CharacterRef(name="A", account="a", level=95, char_class="X", main_skill="Fireball")],
        lambda acc, nm: live_char, normalizer, skill_normalizer, archetyper, LEVEL_BRACKETS,
    )
    reprocessed = reprocess_chars_to_skill_mod_counts(chars, normalizer, skill_normalizer, archetyper, LEVEL_BRACKETS)

    assert len(live_results) == 1 and len(reprocessed) == 1
    live, repro = live_results[0], reprocessed[0]
    assert (repro.slot, repro.main_skill, repro.level_bracket, repro.archetype, repro.stat_id, repro.count, repro.population) == \
           (live.slot, live.main_skill, live.level_bracket, live.archetype, live.stat_id, live.count, live.population)


def test_reprocess_chars_to_skill_mod_counts_handles_jewels_and_multiple_slots(normalizer, archetyper, skill_normalizer):
    items = [_ring(["+87 to maximum Life"]), _jewel("jewel:emerald", ["+23% to Cold Resistance"])]
    chars = [("Fireball", 95, items)]
    results = reprocess_chars_to_skill_mod_counts(chars, normalizer, skill_normalizer, archetyper, LEVEL_BRACKETS)
    slots = {r.slot for r in results}
    assert slots == {"ring", "jewel:emerald"}


def test_reprocess_chars_to_skill_mod_counts_excludes_below_bracket_range(normalizer, archetyper, skill_normalizer):
    chars = [("Fireball", 5, [_ring(["+87 to maximum Life"])])]
    assert reprocess_chars_to_skill_mod_counts(chars, normalizer, skill_normalizer, archetyper, LEVEL_BRACKETS) == []
