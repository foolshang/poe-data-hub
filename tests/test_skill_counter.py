"""Counting one skill group's mods/passives: per character, only into the group
the character was pulled for."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.ninja_chars import CharacterDetail, ItemDetail, ItemMods
from src.processing.normalizer import ModNormalizer
from src.processing.skill_counter import count_skill_group

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ggg_trade2_stats.json"
BRACKETS = [(28, 65), (65, 75), (75, 90), (90, 100)]
LIFE = "+87 to maximum Life"
COLD_RES = "+23% to Cold Resistance"


class _FakeHttp:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url):
        return self._payload


@pytest.fixture
def normalizer(tmp_path) -> ModNormalizer:
    n = ModNormalizer(
        http_client=_FakeHttp(json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))),
        cache_path=tmp_path / "stats_cache.json", unmapped_log_path=tmp_path / "unmapped.log",
    )
    n.load()
    return n


def _item(slot, mods):
    return ItemDetail(slot=slot, base_type="Base", name="x", frame_type_id="Rare", mods=ItemMods(explicit=list(mods)))


def _char(account, level, items):
    return CharacterDetail(account=account, name=account.upper(), level=level, char_class="X",
                           archetype_hint="ignored", items=items)


def _run(normalizer, members, details, excluded=frozenset(), facet_count=1234):
    return count_skill_group(
        "main", "Fireball", members, lambda a, n: details.get(a), normalizer, BRACKETS, excluded, facet_count,
    )


def _member(account, level=95, cls="X", kps=()):
    return (account, account.upper(), level, cls, list(kps))


def test_counts_per_character_with_one_population_base_for_every_slot(normalizer):
    details = {
        "a": _char("a", 95, [_item("ring", [LIFE]), _item("amulet", [LIFE])]),
        "b": _char("b", 96, [_item("ring", [LIFE])]),
        "c": _char("c", 97, [_item("helmet", [COLD_RES])]),
    }
    res = _run(normalizer, [_member("a"), _member("b", 96), _member("c", 97)], details)

    assert res.counted == 3
    ring = next(r for r in res.stat_rows if r["slot"] == "ring")
    assert (ring["count"], ring["population"], ring["slot_holders"], ring["usage_pct"]) == (2, 3, 2, 66.67)
    amulet = next(r for r in res.stat_rows if r["slot"] == "amulet")
    assert (amulet["count"], amulet["population"], amulet["slot_holders"]) == (1, 3, 1)  # base stays 3, not 1
    assert {r["level_bracket"] for r in res.stat_rows} == {"90-100"}


def test_a_stat_on_both_rings_counts_once_per_character(normalizer):
    details = {"a": _char("a", 95, [_item("ring", [LIFE]), _item("ring", [LIFE])])}
    row = _run(normalizer, [_member("a")], details).stat_rows[0]
    assert row["count"] == 1 and row["population"] == 1


def test_jewels_are_counted_per_character_not_per_gem(normalizer):
    """Deliberate difference from the legacy spec-6.4 per-gem rule: two jewels
    of one base carrying the same mod is still one character using it."""
    details = {"a": _char("a", 95, [_item("jewel:emerald", [COLD_RES]), _item("jewel:emerald", [COLD_RES])])}
    row = next(r for r in _run(normalizer, [_member("a")], details).stat_rows if r["slot"] == "jewel:emerald")
    assert row["count"] == 1 and row["population"] == 1 and row["slot_holders"] == 1


def test_population_is_per_level_bracket(normalizer):
    details = {"a": _char("a", 95, [_item("ring", [LIFE])]), "b": _char("b", 80, [_item("ring", [LIFE])])}
    res = _run(normalizer, [_member("a", 95), _member("b", 80)], details)
    by_bracket = {r["level_bracket"]: r for r in res.stat_rows}
    assert by_bracket["90-100"]["population"] == 1 and by_bracket["75-90"]["population"] == 1
    assert {(r["level_bracket"], r["population"]) for r in res.population_rows} == {("90-100", 1), ("75-90", 1)}
    assert all(r["facet_count"] == 1234 for r in res.population_rows)


def test_uses_the_detail_level_and_skips_characters_below_the_first_bracket(normalizer):
    details = {"a": _char("a", 12, [_item("ring", [LIFE])]), "b": _char("b", 95, [_item("ring", [LIFE])])}
    res = _run(normalizer, [_member("a", 95), _member("b")], details)  # list row said 95 for "a"; its detail says 12
    assert res.counted == 1 and res.no_bracket == 1


def test_excluded_class_members_are_not_counted(normalizer):
    details = {"a": _char("a", 95, [_item("ring", [LIFE])]), "b": _char("b", 95, [_item("ring", [LIFE])])}
    res = _run(normalizer, [_member("a"), _member("b", cls="Witch")], details, excluded=frozenset({"Witch"}))
    assert res.counted == 1 and res.cut_by_class == 1
    assert res.stat_rows[0]["population"] == 1


def test_members_without_a_stored_detail_are_skipped_and_reported(normalizer):
    res = _run(normalizer, [_member("a"), _member("ghost")], {"a": _char("a", 95, [_item("ring", [LIFE])])})
    assert res.counted == 1 and res.missing_detail == 1
    assert res.stat_rows[0]["population"] == 1


def test_no_count_threshold_is_applied_every_stat_is_kept(normalizer):
    details = {f"c{i}": _char(f"c{i}", 95, [_item("ring", [LIFE])]) for i in range(3)}
    details["c0"] = _char("c0", 95, [_item("ring", [LIFE]), _item("amulet", [COLD_RES])])
    res = _run(normalizer, [_member(k) for k in details], details)
    amulet = next(r for r in res.stat_rows if r["slot"] == "amulet")
    assert amulet["count"] == 1  # a single character's mod is stored; filtering is a publish-time decision


def test_keypassives_counted_per_character_from_the_search_rows(normalizer):
    details = {"a": _char("a", 95, []), "b": _char("b", 96, [])}
    members = [_member("a", kps=("Dance with Death", "Dance with Death")), _member("b", 96, kps=("Dance with Death", "Ancestral Bond"))]
    res = _run(normalizer, members, details)
    by_kp = {r["keypassive"]: r for r in res.passive_rows}
    assert by_kp["Dance with Death"]["count"] == 2 and by_kp["Dance with Death"]["usage_pct"] == 100.0  # dup counted once
    assert by_kp["Ancestral Bond"]["count"] == 1 and by_kp["Ancestral Bond"]["usage_pct"] == 50.0
    assert all(r["population"] == 2 for r in res.passive_rows)


def test_a_character_in_two_groups_is_counted_into_each_independently(normalizer):
    """The same detail counted for two different skills gives each its own
    ~100 base -- the counter has no notion of 'other skills the character runs'."""
    d = {"a": _char("a", 95, [_item("ring", [LIFE])])}
    one = count_skill_group("main", "Fireball", [_member("a")], lambda a, n: d.get(a), normalizer, BRACKETS, frozenset(), 10)
    two = count_skill_group("spirit", "Ghost Dance", [_member("a")], lambda a, n: d.get(a), normalizer, BRACKETS, frozenset(), 20)
    assert one.stat_rows[0]["population"] == two.stat_rows[0]["population"] == 1


def test_archetype_hint_is_never_consulted(normalizer):
    """The hint is the legacy first-gem guess; counting must not depend on it."""
    a = _char("a", 95, [_item("ring", [LIFE])])
    b = CharacterDetail(account="a", name="A", level=95, char_class="X", archetype_hint=None, items=a.items)
    assert _run(normalizer, [_member("a")], {"a": a}).stat_rows == _run(normalizer, [_member("a")], {"a": b}).stat_rows
