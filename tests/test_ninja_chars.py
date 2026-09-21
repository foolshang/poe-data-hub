"""Unit tests for src/fetchers/ninja_chars.py against a real captured sample.

Sample captured live from poe.ninja's character-detail endpoint on
2026-07-08 (data/samples/ninja_poe2_char_detail.json) — these tests never
hit the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.ninja_builds import SnapshotVersion
from src.fetchers.ninja_chars import (
    NinjaCharsFetcher, _clean_mod_text, _slugify_jewel_base, character_detail_from_dict, character_detail_to_dict,
)

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ninja_poe2_char_detail.json"
POE1_SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ninja_poe1_char_detail.json"

SNAPSHOT = SnapshotVersion(
    league_url="runesofaldur",
    league_name="Runes of Aldur",
    version="0848-20260708-30655",
    snapshot_name="runes-of-aldur",
)

POE1_SNAPSHOT = SnapshotVersion(
    league_url="mirage",
    league_name="Mirage",
    version="1036-20260714-09576",
    snapshot_name="mirage",
)


class _FakeHttpClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def get_json(self, url):
        self.calls += 1
        return self._payload


@pytest.fixture
def sample_payload():
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def fetcher(tmp_path, sample_payload):
    http = _FakeHttpClient(sample_payload)
    return NinjaCharsFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")


def test_clean_mod_text_strips_reference_tags():
    raw = "+24% to all [ElementalDamage|Elemental] [Resistances]"
    assert _clean_mod_text(raw) == "+24% to all Elemental Resistances"

    raw2 = "[Wind|Wind Skills] which can be boosted"
    assert _clean_mod_text(raw2) == "Wind Skills which can be boosted"


def test_fetch_character_basic_fields(fetcher: NinjaCharsFetcher):
    detail = fetcher.fetch_character(SNAPSHOT, "heygyus-0416", "ResurrectGodAura")
    assert detail.account == "heygyus-0416"
    assert detail.name == "ResurrectGodAura"
    assert detail.level == 100
    assert detail.char_class == "Martial Artist"
    assert detail.archetype_hint == "Hollow Focus"


def test_fetch_character_captures_raw_passive_selection(fetcher: NinjaCharsFetcher):
    """Skill-meta phase 3, path A: stored verbatim in char_items even though
    nothing resolves these real GGG node IDs to names yet (no tree dataset
    wired in) -- so a future full-tree phase has historical data to work
    from instead of starting the clock over."""
    detail = fetcher.fetch_character(SNAPSHOT, "heygyus-0416", "ResurrectGodAura")
    assert len(detail.passive_selection) == 109
    assert 331 in detail.passive_selection
    assert all(isinstance(n, int) for n in detail.passive_selection)


def test_fetch_character_passive_selection_defaults_to_empty_when_absent(tmp_path):
    payload = {"account": "a", "name": "n", "level": 1, "class": "c", "items": []}
    http = _FakeHttpClient(payload)
    fetcher = NinjaCharsFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")
    detail = fetcher.fetch_character(SNAPSHOT, "a", "n")
    assert detail.passive_selection == ()


def test_fetch_character_ring_item_mods(fetcher: NinjaCharsFetcher):
    detail = fetcher.fetch_character(SNAPSHOT, "heygyus-0416", "ResurrectGodAura")
    ring_items = [i for i in detail.items if i.slot == "ring"]
    assert len(ring_items) >= 1
    ring = ring_items[0]
    assert ring.name == "The Taming"
    assert any("Elemental Resistances" in m for m in ring.mods.explicit)
    assert all("[" not in m and "]" not in m for m in ring.mods.explicit)


def test_fetch_character_jewels_split_by_base_type(fetcher: NinjaCharsFetcher):
    detail = fetcher.fetch_character(SNAPSHOT, "heygyus-0416", "ResurrectGodAura")
    jewel_slots = {i.slot for i in detail.items if i.slot.startswith("jewel:")}
    assert "jewel:diamond" in jewel_slots
    assert "jewel:emerald" in jewel_slots


def test_fetch_character_skips_rune_socket_items(fetcher: NinjaCharsFetcher):
    detail = fetcher.fetch_character(SNAPSHOT, "heygyus-0416", "ResurrectGodAura")
    assert all(not i.base_type.endswith("Rune") for i in detail.items if i.slot not in ("weapon",))


def test_validation_failure_does_not_raise_generic_exception(tmp_path):
    http = _FakeHttpClient({"account": "x"})  # missing required fields
    fetcher = NinjaCharsFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")
    with pytest.raises(ValueError, match="validation failed"):
        fetcher.fetch_character(SNAPSHOT, "x", "y")
    dumped = list((tmp_path / "failed").glob("*.json"))
    assert len(dumped) == 1


def test_character_detail_round_trips_through_the_store_serializers(fetcher: NinjaCharsFetcher):
    """The shared SQLite character cache stores `character_detail_to_dict` JSON
    and rebuilds with `character_detail_from_dict` -- nothing may be lost."""
    detail = fetcher.fetch_character(SNAPSHOT, "heygyus-0416", "ResurrectGodAura")
    restored = character_detail_from_dict(json.loads(json.dumps(character_detail_to_dict(detail))))
    assert restored == detail
    assert restored.items and restored.passive_selection


def test_unknown_game_raises_at_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown game"):
        NinjaCharsFetcher(http_client=_FakeHttpClient({}), game="poe3", failed_responses_dir=tmp_path)


# ── phase 10: jewel base-type slugification (both games) ──────────────────

def test_slugify_jewel_base_poe1_color_jewel():
    assert _slugify_jewel_base("Cobalt Jewel") == "jewel:cobalt"
    assert _slugify_jewel_base("Crimson Jewel") == "jewel:crimson"
    assert _slugify_jewel_base("Viridian Jewel") == "jewel:viridian"
    assert _slugify_jewel_base("Prismatic Jewel") == "jewel:prismatic"


def test_slugify_jewel_base_poe1_cluster_collapses_size_prefix():
    assert _slugify_jewel_base("Small Cluster Jewel") == "jewel:cluster"
    assert _slugify_jewel_base("Medium Cluster Jewel") == "jewel:cluster"
    assert _slugify_jewel_base("Large Cluster Jewel") == "jewel:cluster"


def test_slugify_jewel_base_poe1_abyss():
    assert _slugify_jewel_base("Abyss Jewel") == "jewel:abyss"


def test_slugify_jewel_base_poe1_named_abyss_variants():
    # Discovered live (phase 10 meta smoke test, 2026-07-15): PoE1 abyss
    # jewels don't share one generic "Abyss Jewel" baseType -- each named
    # variant is its own baseType string, so they naturally split into their
    # own buckets rather than collapsing into a single "jewel:abyss".
    assert _slugify_jewel_base("Searching Eye Jewel") == "jewel:searchingeye"
    assert _slugify_jewel_base("Murderous Eye Jewel") == "jewel:murderouseye"
    assert _slugify_jewel_base("Ghastly Eye Jewel") == "jewel:ghastlyeye"
    assert _slugify_jewel_base("Hypnotic Eye Jewel") == "jewel:hypnoticeye"


def test_slugify_jewel_base_poe1_timeless():
    assert _slugify_jewel_base("Timeless Jewel") == "jewel:timeless"


def test_slugify_jewel_base_poe2_unchanged():
    assert _slugify_jewel_base("Diamond") == "jewel:diamond"
    assert _slugify_jewel_base("Time-Lost Emerald") == "jewel:timelostemerald"


# ── phase 10: PoE1 character fetch (real sample) ───────────────────────────

@pytest.fixture
def poe1_fetcher(tmp_path):
    payload = json.loads(POE1_SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    return NinjaCharsFetcher(http_client=http, game="poe1", failed_responses_dir=tmp_path / "failed")


def test_fetch_poe1_character_basic_fields(poe1_fetcher: NinjaCharsFetcher):
    detail = poe1_fetcher.fetch_character(POE1_SNAPSHOT, "Poteitik-3151", "GarfSkinEnjoyerrr")
    assert detail.char_class == "Pathfinder"
    assert detail.level == 100
    assert detail.archetype_hint == "Vaal Caustic Arrow"


def test_fetch_poe1_character_jewels_split_by_clean_base_type(poe1_fetcher: NinjaCharsFetcher):
    detail = poe1_fetcher.fetch_character(POE1_SNAPSHOT, "Poteitik-3151", "GarfSkinEnjoyerrr")
    jewel_slots = {i.slot for i in detail.items if i.slot.startswith("jewel:")}
    assert "jewel:cobalt" in jewel_slots
    assert "jewel:viridian" in jewel_slots
    assert "jewel:prismatic" in jewel_slots
    assert "jewel:cluster" in jewel_slots
    assert "jewel:cobaltjewel" not in jewel_slots  # the un-stripped bug this fix prevents


def test_fetch_poe1_character_no_focus_slot(poe1_fetcher: NinjaCharsFetcher):
    detail = poe1_fetcher.fetch_character(POE1_SNAPSHOT, "Poteitik-3151", "GarfSkinEnjoyerrr")
    assert all(i.slot != "focus" for i in detail.items)
