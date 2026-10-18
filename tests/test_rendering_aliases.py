"""Tests for src/processing/rendering_aliases.py (RePoE-driven aliases).

The entries below copy the SHAPE of real RePoE stat_translations entries (checked against the PoE1 file on
2026-10-19): ordered `English` variants, each with `condition`, `index_handlers`, `string`, plus `trade_stats`.
"""
import pytest

from src.processing.rendering_aliases import AliasIndex, build_aliases, check_not_shrunk


def variant(string, lo=None, hi=None, handlers=None):
    return {"condition": [{"min": lo, "max": hi, "negated": None}], "index_handlers": [handlers or []],
            "string": string, "format": ["#"], "reminder_text": None, "is_markup": None}


def entry(ids, trade, *variants):
    return {"ids": ids, "trade_stats": [{"id": t, "text": "x", "type": t.split(".")[0], "option": None} for t in trade],
            "English": list(variants), "hidden": None}


def build(entries, table):
    payload = build_aliases("poe1", entries, {k: dict(v) for k, v in table.items()})
    return payload, AliasIndex(payload)


def hit(index, kind, text):
    return index.candidates(kind, text)


# -- singular/plural: just two variants selected by condition ------------------------------------

def test_singular_and_plural_wordings_come_from_the_conditions():
    e = entry(["gain_charges"], ["explicit.stat_100"],
              variant("Gain {0} Charge when Hit", 1, 1), variant("Gain {0} Charges when Hit", 2, None))
    _, idx = build([e], {"explicit": {"explicit.stat_100": "Gain # Charge when Hit"}})
    assert hit(idx, "explicit", "Gain 3 Charges when Hit") == {"100": ([3.0], False)}
    assert hit(idx, "explicit", "Gain 1 Charges when Hit") == {}          # 1 is the singular's range, not the plural's


# -- negate: the stored sign follows the wording the TABLE lists -----------------------------------

def test_mirrored_wording_is_stored_negative_relative_to_the_table_wording():
    e = entry(["ailment_duration"], ["explicit.stat_200"],
              variant("{0}% reduced Elemental Ailment Duration on you", 1, None),
              variant("{0}% increased Elemental Ailment Duration on you", None, -1, ["negate"]))
    payload, idx = build([e], {"explicit": {"explicit.stat_200": "#% reduced Elemental Ailment Duration on you"}})
    assert hit(idx, "explicit", "50% increased Elemental Ailment Duration on you") == {"200": ([-50.0], False)}
    assert len(payload["aliases"]) == 1          # the 'reduced' wording equals the table text: the exact path has it


def test_when_the_table_lists_the_negate_wording_the_other_side_is_the_negative_one():
    e = entry(["x"], ["explicit.stat_201"],
              variant("{0}% reduced Thing", 1, None), variant("{0}% increased Thing", None, -1, ["negate"]))
    _, idx = build([e], {"explicit": {"explicit.stat_201": "#% increased Thing"}})
    assert hit(idx, "explicit", "7% reduced Thing") == {"201": ([-7.0], False)}


def test_a_value_outside_the_variants_condition_does_not_match():
    e = entry(["x"], ["explicit.stat_202"],
              variant("{0}% reduced Thing", 1, None), variant("{0}% increased Thing", None, -1, ["negate"]))
    _, idx = build([e], {"explicit": {"explicit.stat_202": "#% reduced Thing"}})
    assert hit(idx, "explicit", "0% increased Thing") == {}                  # internal 0 satisfies neither [1..] nor [..-1]


# -- units: stored numbers are the DISPLAYED ones, never the internal ones -----------------------------

def test_every_second_is_one_not_one_thousand():
    e = entry(["void_ms"], ["explicit.stat_300"],
              variant("Gain a Void Charge every second", 1000, 1000),
              variant("Gain a Void Charge every {0} seconds", None, None, ["milliseconds_to_seconds"]))
    _, idx = build([e], {"explicit": {"explicit.stat_300": "Gain a Void Charge every # seconds"}})
    values, dep = hit(idx, "explicit", "Gain a Void Charge every second")["300"]
    assert values == [1.0]                       # the unit bug of the first prototype gave 1000.0
    assert dep is False


def test_a_displayed_seconds_value_passes_the_internal_millisecond_condition():
    e = entry(["linger_ms"], ["explicit.stat_301"],
              variant("Linger for 1 second", 1000, 1000),
              variant("Linger for {0} seconds", None, None, ["milliseconds_to_seconds"]))
    _, idx = build([e], {"explicit": {"explicit.stat_301": "Linger for 1 second"}})
    assert hit(idx, "explicit", "Linger for 4 seconds") == {"301": ([4.0], False)}


def test_divide_by_one_hundred_keeps_the_displayed_number():
    e = entry(["rate"], ["explicit.stat_302"],
              variant("{0}% more Rate", 1, None, ["divide_by_one_hundred"]), variant("{0}% less Rate", None, -1, ["negate", ]))
    # the second variant's handler list is ["negate"]; the first scales by 100 (internal 150 -> shown 1.5)
    _, idx = build([e], {"explicit": {"explicit.stat_302": "#% increased Rate"}})
    assert hit(idx, "explicit", "1.5% more Rate") == {"302": ([1.5], False)}


def test_an_unsupported_handler_makes_the_whole_entry_unprovable_for_implied_values():
    e = entry(["weird"], ["explicit.stat_303"],
              variant("Plain wording", 5, 5), variant("{0} per minute", None, None, ["per_minute_to_per_second"]))
    payload, idx = build([e], {"explicit": {"explicit.stat_303": "# per minute"}})
    assert hit(idx, "explicit", "Plain wording") == {}                      # unit unknown -> not guessed
    assert payload["stats"]["skipped_implied_value_entry_not_provable"] >= 1


# -- implied values: exactly three provable shapes ------------------------------------------------------

def test_shape_one_a_single_value_condition():
    e = entry(["lvl"], ["explicit.stat_400"], variant("Trigger Level 20 Thing", 2000, 2000), variant("{0}% chance to Trigger Level 1 Thing", 1, 99))
    _, idx = build([e], {"explicit": {"explicit.stat_400": "#% chance to Trigger Level 1 Thing"}})
    assert hit(idx, "explicit", "Trigger Level 20 Thing") == {"400": ([2000.0], False)}


def test_shape_two_open_above_with_a_complementary_sibling_is_the_100_percent_case():
    e = entry(["chance"], ["explicit.stat_401"], variant("{0}% chance to Ignore Stuns", 1, 99), variant("Ignore Stuns", 100, None))
    _, idx = build([e], {"explicit": {"explicit.stat_401": "#% chance to Ignore Stuns"}})
    assert hit(idx, "explicit", "Ignore Stuns") == {"401": ([100.0], False)}


def test_shape_two_without_a_complementary_sibling_is_not_provable():
    e = entry(["lonely"], ["explicit.stat_402"], variant("Does the thing", 100, None))
    payload, idx = build([e], {"explicit": {"explicit.stat_402": "Something else"}})
    assert hit(idx, "explicit", "Does the thing") == {}


def test_shape_three_lucky_and_unlucky_map_to_one_stat_without_a_stored_number():
    e = entry(["block_luck"], ["explicit.stat_403"], variant("Chance to Block is Lucky", 1, None), variant("Chance to Block is Unlucky", None, -1))
    _, idx = build([e], {"explicit": {"explicit.stat_403": "Chance to Block is Unlucky"}})
    assert hit(idx, "explicit", "Chance to Block is Lucky") == {"403": ([], False)}


def test_one_sided_upper_bound_without_a_mirror_is_not_an_implied_value():
    e = entry(["impale"], ["explicit.stat_404"], variant("Critical Strikes inflict Impale", None, 99))
    _, idx = build([e], {"explicit": {"explicit.stat_404": "Something else"}})
    assert hit(idx, "explicit", "Critical Strikes inflict Impale") == {}      # the prototype wrongly produced 99


def test_none_none_default_branch_is_never_mapped():
    e = entry(["shrine"], ["explicit.stat_405"],
              variant("{0}% chance to Trigger Level 1 Shrine", 1, 99), variant("Trigger Level 20 Shrine", 2000, 2000),
              variant("Trigger Level 1 Shrine", None, None))
    payload, idx = build([e], {"explicit": {"explicit.stat_405": "#% chance to Trigger Level 1 Shrine"}})
    assert hit(idx, "explicit", "Trigger Level 1 Shrine") == {}               # value unknown: stays unmapped, not stored as a guess
    assert payload["stats"]["skipped_implied_value_not_provable"] >= 1


# -- kinds, redundancy, deprecated, markup ----------------------------------------------------------------

def test_an_alias_only_applies_to_kinds_where_the_stat_exists_in_the_ggg_table():
    e = entry(["x"], ["implicit.stat_500", "explicit.stat_500"], variant("Has {0} Sockets", 2, None), variant("Has 1 Socket", 1, 1))
    _, idx = build([e], {"explicit": {"explicit.stat_500": "Has 1 Socket"}, "implicit": {}})
    assert hit(idx, "explicit", "Has 2 Sockets") == {"500": ([2.0], False)}
    assert hit(idx, "implicit", "Has 2 Sockets") == {}
    assert hit(idx, "fractured", "Has 2 Sockets") == {}


def test_a_deprecated_stat_is_flagged_and_its_twin_is_kept_even_when_the_text_equals_the_table():
    old = entry(["old_do_not_use_spell_block"], ["explicit.stat_600"], variant("{0}% Chance to Block", None, None))
    cur = entry(["spell_block"], ["explicit.stat_601"], variant("{0}% Chance to Block", None, None))
    payload, idx = build([old, cur], {"explicit": {"explicit.stat_600": "#% Chance to Block", "explicit.stat_601": "+#% Chance to Block"}})
    assert payload["deprecated"] == ["600"]
    got = hit(idx, "explicit", "30% Chance to Block")
    assert got["600"][1] is True and got["601"][1] is False


def test_poe2_markup_is_stripped_from_the_wording():
    e = entry(["armour"], ["explicit.stat_700"], variant("{0}% increased [Armour|Armour] and [Evasion]", 1, None))
    _, idx = build([e], {"explicit": {"explicit.stat_700": "something else"}})
    assert hit(idx, "explicit", "5% increased Armour and Evasion") == {"700": ([5.0], False)}


def test_multi_stat_and_repeated_placeholder_variants_are_skipped_not_guessed():
    e = entry(["a", "b"], ["explicit.stat_800"], variant("Adds {0} to {0} Damage", 1, None))
    payload, idx = build([e], {"explicit": {"explicit.stat_800": "Adds # to # Damage"}})
    assert payload["aliases"] == []
    assert payload["stats"]["skipped_variant_multi_stat_or_repeated_placeholder"] == 1


# -- the refresh guard ------------------------------------------------------------------------------------

def _payload(n_aliases, n_dep=0):
    return {"aliases": [{}] * n_aliases, "deprecated": [str(i) for i in range(n_dep)]}


def test_guard_rejects_an_empty_payload():
    with pytest.raises(ValueError):
        check_not_shrunk(_payload(0), None)


def test_guard_rejects_a_large_drop_against_the_last_good_file():
    with pytest.raises(ValueError, match="aliases shrank"):
        check_not_shrunk(_payload(80), _payload(100))
    with pytest.raises(ValueError, match="deprecated shrank"):
        check_not_shrunk(_payload(100, 10), _payload(100, 28))


def test_guard_accepts_growth_and_small_drops():
    check_not_shrunk(_payload(100), None)
    check_not_shrunk(_payload(95), _payload(100))
    check_not_shrunk(_payload(150), _payload(100))


def test_unreadable_payload_gives_an_empty_index_not_an_exception():
    assert AliasIndex({"schema_version": 99}).size == 0
    assert AliasIndex({"schema_version": 1, "aliases": [{"garbage": 1}]}).size == 0
    assert AliasIndex(None).candidates("explicit", "anything") == {}


def test_a_trade_id_shared_with_a_current_stat_is_not_deprecated():
    # GGG gives one trade id to several internal stats: 24 of the 28 ids behind old_* stats are shared like this.
    old = entry(["old_dagger_implicit_crit"], ["implicit.stat_900"], variant("{0}% increased Crit", None, None))
    cur = entry(["dagger_implicit_crit"], ["implicit.stat_900"], variant("{0}% increased Crit", None, None))
    only_old = entry(["old_do_not_use_thing"], ["implicit.stat_901"], variant("{0}% more Thing", None, None))
    payload, _ = build([old, cur, only_old], {"implicit": {"implicit.stat_900": "#% increased Crit", "implicit.stat_901": "#% more Thing"}})
    assert payload["deprecated"] == ["901"]
