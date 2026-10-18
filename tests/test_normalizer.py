"""Unit tests for src/processing/normalizer.py against the real trade2 stats sample.

Sample was captured live from https://www.pathofexile.com/api/trade2/data/stats
on 2026-07-08 (data/samples/ggg_trade2_stats.json) per the "test once, save
sample" rule in spec section 11 — these tests never hit the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.processing.normalizer import ModNormalizer

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ggg_trade2_stats.json"


class _FakeHttpClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def get_json(self, url):
        self.calls += 1
        return self._payload


@pytest.fixture
def normalizer(tmp_path) -> ModNormalizer:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    n = ModNormalizer(
        http_client=http,
        cache_path=tmp_path / "stats_cache.json",
        unmapped_log_path=tmp_path / "unmapped_mods.log",
    )
    n.load()
    return n


def test_simple_explicit_life_mod(normalizer: ModNormalizer):
    result = normalizer.normalize("+87 to maximum Life", "explicit")
    assert len(result) == 1
    m = result[0]
    assert m.stat_id == "explicit.stat_3299347043"
    assert m.mod_kind == "explicit"
    assert m.values == [87.0]


def test_percent_resistance_mod(normalizer: ModNormalizer):
    result = normalizer.normalize("+23% to Cold Resistance", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_4220027924"
    assert result[0].values == [23.0]


def test_two_value_damage_range_mod(normalizer: ModNormalizer):
    result = normalizer.normalize("Adds 5 to 10 Physical Damage to Attacks", "explicit")
    assert len(result) == 1
    m = result[0]
    assert m.stat_id == "explicit.stat_3032590688"
    assert m.values == [5.0, 10.0]


def test_literal_plus_sign_baked_into_template(normalizer: ModNormalizer):
    # "+#% Monster Elemental Resistances" bakes a literal "+" into the
    # template itself (always-positive map mod, no "-" counterpart exists),
    # so naive sign-swallowing skeletonization would miss it.
    result = normalizer.normalize("+15% Monster Elemental Resistances", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_1054098949"
    assert result[0].values == [15.0]


def test_implicit_mod_kind_is_respected(normalizer: ModNormalizer):
    # Same display text can exist under a different stat_id per mod_kind.
    result = normalizer.normalize("+18% increased Rarity of Items found", "implicit")
    assert len(result) == 1
    assert result[0].stat_id == "implicit.stat_3917489142"
    assert result[0].mod_kind == "implicit"


def test_wrong_mod_kind_does_not_match(normalizer: ModNormalizer):
    # "#% reduced Elemental Ailment Duration on you" exists only under
    # "implicit" in the sample table, not under "fractured" -> unmapped.
    result = normalizer.normalize("20% reduced Elemental Ailment Duration on you", "fractured")
    assert result == []


def test_unmapped_mod_is_logged_not_raised(normalizer: ModNormalizer, tmp_path):
    result = normalizer.normalize("This is definitely not a real mod #", "explicit")
    assert result == []
    log_path = tmp_path / "unmapped_mods.log"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["mod_kind"] == "explicit"
    assert "not a real mod" in entry["text"]


def test_repeated_unmapped_mod_is_logged_once_but_still_counted(normalizer: ModNormalizer, tmp_path, caplog):
    """2026-10-15: logging every occurrence put ~260k WARNING lines in one
    hour into the journal. Each distinct unmapped mod is written once per
    normalizer instance; repeats only bump the counters (unmapped_pct and the
    summary still see them)."""
    with caplog.at_level("INFO"):
        for _ in range(500):
            normalizer.normalize("This is definitely not a real mod #", "explicit")
        normalizer.normalize("Another fake mod #", "explicit")
        normalizer.normalize("This is definitely not a real mod #", "implicit")  # different kind = distinct
        normalizer.log_unmapped_summary()

    lines = (tmp_path / "unmapped_mods.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    warnings = [r for r in caplog.records if "unmapped mod (" in r.getMessage()]
    assert len(warnings) == 3
    assert normalizer.unmapped_count == 502
    assert normalizer.unmapped_unique_count == 3
    summary = [r.getMessage() for r in caplog.records if "unmapped mod occurrences" in r.getMessage()]
    assert len(summary) == 1 and "502" in summary[0] and "500x" in summary[0]


def test_unmapped_summary_is_silent_when_nothing_was_unmapped(normalizer: ModNormalizer, caplog):
    with caplog.at_level("INFO"):
        normalizer.log_unmapped_summary()
    assert not [r for r in caplog.records if "unmapped" in r.getMessage()]


def test_stale_cache_used_when_refresh_fails(tmp_path):
    """Regression test for the 2026-07-10 VM deploy incident: GGG's trade2
    API returns a persistent 403 to some cloud-hosted IPs. A refresh failure
    must fall back to whatever's on disk (even past its TTL) instead of
    crashing the whole meta job."""
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    cache_path = tmp_path / "stats_cache.json"

    http1 = _FakeHttpClient(payload)
    n1 = ModNormalizer(http_client=http1, cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n1.load()

    # Make the cache look expired, then simulate the endpoint being blocked.
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    envelope["fetched_at"] = 0
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    class _FailingHttpClient:
        def get_json(self, url):
            raise RuntimeError("403 Forbidden")

    n2 = ModNormalizer(http_client=_FailingHttpClient(), cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n2.load()  # must not raise

    result = n2.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


def test_no_cache_and_refresh_fails_raises(tmp_path):
    class _FailingHttpClient:
        def get_json(self, url):
            raise RuntimeError("403 Forbidden")

    n = ModNormalizer(
        http_client=_FailingHttpClient(), cache_path=tmp_path / "nope.json", unmapped_log_path=tmp_path / "u.log"
    )
    with pytest.raises(RuntimeError, match="403"):
        n.load()


def test_ggg_direct_writes_disk_cache_for_later_fallback(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    cache_path = tmp_path / "stats_cache.json"

    n1 = ModNormalizer(http_client=http, cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n1.load()
    assert http.calls == 1
    assert n1.stats_source == "ggg_direct"
    assert cache_path.exists()

    result = n1.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


class _FirebaseFakeHttpClient:
    """Distinguishes the Firebase stats-cache URL from the GGG endpoint so
    tests can tell which tier actually served the data."""

    def __init__(self, firebase_envelope=None, ggg_payload=None, firebase_should_fail=False):
        self._firebase_envelope = firebase_envelope
        self._ggg_payload = ggg_payload
        self._firebase_should_fail = firebase_should_fail
        self.firebase_calls = 0
        self.ggg_calls = 0

    def get_json(self, url):
        if "firebase" in url or "storage.googleapis.com" in url:
            self.firebase_calls += 1
            if self._firebase_should_fail:
                raise RuntimeError("404 not found")
            return self._firebase_envelope
        self.ggg_calls += 1
        if self._ggg_payload is None:
            raise RuntimeError("403 Forbidden")
        return self._ggg_payload


def test_firebase_used_first_when_configured(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FirebaseFakeHttpClient(firebase_envelope={"fetched_at": 1720000000.0, "data": payload})
    n = ModNormalizer(
        http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n.load()
    assert http.firebase_calls == 1
    assert http.ggg_calls == 0
    assert n.stats_source == "firebase"
    assert n.stats_fetched_at == 1720000000.0
    result = n.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


def test_falls_through_to_ggg_when_firebase_unavailable(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FirebaseFakeHttpClient(firebase_should_fail=True, ggg_payload=payload)
    n = ModNormalizer(
        http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n.load()
    assert http.firebase_calls == 1
    assert http.ggg_calls == 1
    assert n.stats_source == "ggg_direct"


def test_firebase_bad_shape_falls_through_to_ggg(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FirebaseFakeHttpClient(firebase_envelope={"fetched_at": 1720000000.0, "data": {"nope": True}}, ggg_payload=payload)
    n = ModNormalizer(
        http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n.load()
    assert n.stats_source == "ggg_direct"


def test_no_firebase_configured_goes_straight_to_ggg(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    n = ModNormalizer(http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log")
    n.load()
    assert n.stats_source == "ggg_direct"
    assert http.calls == 1


def test_disk_cache_is_last_resort_when_firebase_and_ggg_both_fail(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    cache_path = tmp_path / "c.json"

    # Seed the disk cache via a working GGG-direct load first.
    n1 = ModNormalizer(http_client=_FakeHttpClient(payload), cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n1.load()

    # Now both Firebase and GGG fail -- must fall back to the seeded disk cache.
    http2 = _FirebaseFakeHttpClient(firebase_should_fail=True, ggg_payload=None)
    n2 = ModNormalizer(
        http_client=http2, cache_path=cache_path, unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n2.load()  # must not raise
    assert n2.stats_source == "disk_cache_stale"
    result = n2.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


def test_multiline_hybrid_mod_matches_as_single_entry(normalizer: ModNormalizer):
    # "Adds Irradiated to a Map \n# use remaining" is stored as ONE two-line
    # template under a single stat_id — normalize() must try the whole raw
    # block first (longest-template-first) rather than splitting into two
    # lines and failing to match either.
    result = normalizer.normalize("Adds Irradiated to a Map \n3 use remaining", "implicit")
    assert len(result) == 1
    assert result[0].stat_id == "implicit.stat_4041853756"
    assert result[0].values == [3.0]


def test_plural_slot_text_matches_singular_template(normalizer: ModNormalizer):
    # Template is "Has # Charm Slot" (singular) but real item text pluralizes.
    result = normalizer.normalize("Has 3 Charm Slots", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_1416292992"
    assert result[0].values == [3.0]


def test_reduced_word_matches_increased_template_with_negated_value(normalizer: ModNormalizer):
    # Template only has "#% increased Attribute Requirements" -- the game
    # displays a negative roll as "reduced" instead of "increased -35%".
    result = normalizer.normalize("35% reduced Attribute Requirements", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_3639275092"
    assert result[0].values == [-35.0]


def test_processed_and_unmapped_counters(normalizer: ModNormalizer):
    normalizer.normalize("+87 to maximum Life", "explicit")
    normalizer.normalize("This is definitely not a real mod #", "explicit")
    normalizer.normalize("+23% to Cold Resistance", "explicit")
    assert normalizer.processed_count == 3
    assert normalizer.unmapped_count == 1


def test_stat_dictionary_embeds_all_known_kinds(normalizer: ModNormalizer):
    entries = normalizer.stat_dictionary()
    kinds = {e["mod_kind"] for e in entries}
    assert kinds == {"explicit", "implicit", "fractured"}
    assert any(e["stat_id"] == "explicit.stat_3299347043" for e in entries)


def test_unknown_game_raises_at_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown game"):
        ModNormalizer(
            http_client=_FakeHttpClient({}), game="poe3",
            cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log",
        )


# ── phase 10: PoE1 uses trade/data/stats (not trade2), same shape ─────────

POE1_SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "poe1_trade_stats.json"


@pytest.fixture
def poe1_normalizer(tmp_path) -> ModNormalizer:
    payload = json.loads(POE1_SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    n = ModNormalizer(
        http_client=http,
        game="poe1",
        cache_path=tmp_path / "poe1_stats_cache.json",
        unmapped_log_path=tmp_path / "poe1_unmapped_mods.log",
    )
    n.load()
    return n


def test_poe1_normalizer_loads_real_sample(poe1_normalizer: ModNormalizer):
    assert poe1_normalizer.stats_source == "ggg_direct"
    kinds = {e["mod_kind"] for e in poe1_normalizer.stat_dictionary()}
    assert {"explicit", "implicit", "fractured"} <= kinds


def test_poe1_normalizer_matches_simple_life_mod(poe1_normalizer: ModNormalizer):
    result = poe1_normalizer.normalize("+87 to maximum Life", "explicit")
    assert len(result) == 1
    assert result[0].mod_kind == "explicit"
    assert result[0].values == [87.0]


def test_poe1_and_poe2_normalizers_use_different_stats_urls():
    from src.processing.normalizer import STATS_URL_BY_GAME

    assert STATS_URL_BY_GAME["poe1"] != STATS_URL_BY_GAME["poe2"]
    assert "trade2" in STATS_URL_BY_GAME["poe2"]
    assert "trade2" not in STATS_URL_BY_GAME["poe1"]
    assert "trade/" in STATS_URL_BY_GAME["poe1"]


# -- (Local) suffix aliasing ----------------------------------------------
# GGG lists weapon/armour-local mods as "... (Local)" but the item text has no
# suffix. A suffix-less text is aliased to the local stat only when that is
# unambiguous; anything that already matched must never change.

def _normalizer_for(tmp_path, groups: dict) -> ModNormalizer:
    payload = {"result": [
        {"id": kind, "entries": [{"id": sid, "text": text} for sid, text in entries]}
        for kind, entries in groups.items()
    ]}
    n = ModNormalizer(http_client=_FakeHttpClient(payload), cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log")
    n.load()
    return n


def test_local_only_template_is_matched_without_the_suffix(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [("explicit.stat_L", "#% increased Energy Shield (Local)")]})
    result = n.normalize("100% increased Energy Shield", "explicit")
    assert [m.stat_id for m in result] == ["explicit.stat_L"]
    assert result[0].values == [100.0]
    assert result[0].template == "#% increased Energy Shield (Local)"
    assert n.local_alias_hits == 1


def test_local_with_a_global_twin_is_not_aliased_and_global_keeps_winning(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [
        ("explicit.stat_G", "+# to maximum Energy Shield"),
        ("explicit.stat_L", "+# to maximum Energy Shield (Local)"),
    ]})
    assert [m.stat_id for m in n.normalize("+40 to maximum Energy Shield", "explicit")] == ["explicit.stat_G"]
    assert n.local_alias_hits == 0
    assert "+# to maximum Energy Shield" not in n._local_alias["explicit"]


def test_two_locals_sharing_one_suffixless_text_are_not_aliased(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [
        ("explicit.stat_1", "#% increased Thing (Local)"),
        ("explicit.stat_2", "#% increased Thing  (Local)"),
    ]})
    assert n.normalize("5% increased Thing", "explicit") == []


def test_local_alias_is_scoped_to_its_own_mod_kind(tmp_path):
    n = _normalizer_for(tmp_path, {
        "implicit": [("implicit.stat_L", "#% increased Energy Shield (Local)")],
        "explicit": [("explicit.stat_X", "+# to maximum Life")],
    })
    assert [m.stat_id for m in n.normalize("10% increased Energy Shield", "implicit")] == ["implicit.stat_L"]
    assert n.normalize("10% increased Energy Shield", "explicit") == []


def test_existing_fallback_still_wins_before_the_local_alias(tmp_path):
    # "Has 2 Slots": the plural fallback reaches the global "Has # Slot" first; the alias
    # for "Has # Slots (Local)" must not hijack a text the older rules already matched.
    n = _normalizer_for(tmp_path, {"explicit": [
        ("explicit.stat_G", "Has # Slot"),
        ("explicit.stat_L", "Has # Slots (Local)"),
    ]})
    assert [m.stat_id for m in n.normalize("Has 2 Slots", "explicit")] == ["explicit.stat_G"]
    assert n.local_alias_hits == 0


# -- templates with a fixed number baked into the text ------------------------
# GGG keeps "per 100 Strength" and "per 15 Strength" as separate stats, so a
# fixed number must match exactly; `values` holds only the '#' positions.

def test_pure_fixed_number_template_matches_only_that_number(tmp_path):
    n = _normalizer_for(tmp_path, {"implicit": [("implicit.stat_S1", "Has 1 Socket")]})
    assert [m.stat_id for m in n.normalize("Has 1 Socket", "implicit")] == ["implicit.stat_S1"]
    assert n.normalize("Has 2 Sockets", "implicit") == []   # not in the table: must NOT be collapsed onto "1 Socket"
    assert n.literal_hits == 1


def test_fixed_number_is_not_part_of_the_captured_values(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [("explicit.stat_E", "#% increased Attack Damage per 450 Armour")]})
    result = n.normalize("12% increased Attack Damage per 450 Armour", "explicit")
    assert [m.stat_id for m in result] == ["explicit.stat_E"]
    assert result[0].values == [12.0]    # 450 is part of the stat, not a roll


def test_templates_that_differ_only_by_their_fixed_number_stay_separate(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [
        ("explicit.stat_100", "#% increased Damage per 100 Strength"),
        ("explicit.stat_15", "#% increased Damage per 15 Strength"),
    ]})
    assert [m.stat_id for m in n.normalize("3% increased Damage per 100 Strength", "explicit")] == ["explicit.stat_100"]
    assert [m.stat_id for m in n.normalize("4% increased Damage per 15 Strength", "explicit")] == ["explicit.stat_15"]
    assert n.normalize("4% increased Damage per 20 Strength", "explicit") == []


def test_two_stats_matching_the_same_fixed_text_are_left_unmapped(tmp_path):
    # "4" and "4.0" are the same fixed number but two different table entries: the text matches
    # both stat_ids, so guessing one would be wrong half the time -> stay unmapped, and count it.
    n = _normalizer_for(tmp_path, {"explicit": [
        ("explicit.stat_a", "#% chance for 4 seconds on Kill"),
        ("explicit.stat_b", "#% chance for 4.0 seconds on Kill"),
    ]})
    assert n.normalize("9% chance for 4 seconds on Kill", "explicit") == []
    assert n.literal_ambiguous == 1
    assert n.literal_hits == 0


def test_all_placeholder_twin_keeps_winning_over_a_fixed_number_template(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [
        ("explicit.stat_var", "Gain # Fear Incarnate"),
        ("explicit.stat_one", "Gain 1 Fear Incarnate"),
    ]})
    assert [m.stat_id for m in n.normalize("Gain 1 Fear Incarnate", "explicit")] == ["explicit.stat_var"]
    assert n.literal_hits == 0


def test_literal_sign_template_is_not_reached_by_a_negative_roll(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [("explicit.stat_P", "+#% to Chance for 4 seconds")]})
    assert [m.stat_id for m in n.normalize("+10% to Chance for 4 seconds", "explicit")] == ["explicit.stat_P"]
    assert n.normalize("-10% to Chance for 4 seconds", "explicit") == []   # negative values of +# templates are a separate rule


# -- negative rolls of templates that bake a '+' into the text ---------------

def test_negative_roll_matches_a_plus_template_and_keeps_its_sign(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [("explicit.stat_R", "+#% to Fire Resistance")]})
    pos = n.normalize("+30% to Fire Resistance", "explicit")
    neg = n.normalize("-30% to Fire Resistance", "explicit")
    assert [m.stat_id for m in pos] == ["explicit.stat_R"] and pos[0].values == [30.0]
    assert [m.stat_id for m in neg] == ["explicit.stat_R"]
    assert neg[0].values == [-30.0]          # the roll really was negative; it is not stored as +30
    assert n.negative_flip_hits == 1


def test_negative_roll_only_flips_the_numbers_that_were_negative(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [("explicit.stat_M", "+# to Thing per +# Other")]})
    result = n.normalize("-2 to Thing per +5 Other", "explicit")
    assert result[0].values == [-2.0, 5.0]


def test_a_hyphen_or_range_is_not_treated_as_a_negative_sign(tmp_path):
    n = _normalizer_for(tmp_path, {"explicit": [("explicit.stat_D", "+# to Something")]})
    assert n.normalize("Adds 1-5 to Something", "explicit") == []
    assert n.negative_flip_hits == 0


def test_negative_flip_never_overrides_a_template_that_already_matched(tmp_path):
    # "#% to Cold Resistance" (no plus) already accepts "-30%": the flip path must not run for it.
    n = _normalizer_for(tmp_path, {"explicit": [
        ("explicit.stat_plain", "#% to Cold Resistance"),
        ("explicit.stat_plus", "+#% to Cold Resistance"),
    ]})
    result = n.normalize("-30% to Cold Resistance", "explicit")
    assert [m.stat_id for m in result] == ["explicit.stat_plain"]
    assert result[0].values == [-30.0]
    assert n.negative_flip_hits == 0
