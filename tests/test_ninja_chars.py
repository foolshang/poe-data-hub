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
from src.fetchers.ninja_chars import DedupedCharacterCache, NinjaCharsFetcher, _clean_mod_text

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ninja_poe2_char_detail.json"

SNAPSHOT = SnapshotVersion(
    league_url="runesofaldur",
    league_name="Runes of Aldur",
    version="0848-20260708-30655",
    snapshot_name="runes-of-aldur",
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
    assert detail.main_skill == "Hollow Focus"


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


def test_deduped_cache_fetches_once_per_character(fetcher: NinjaCharsFetcher):
    cache = DedupedCharacterCache(fetcher, SNAPSHOT)
    d1 = cache.get("heygyus-0416", "ResurrectGodAura")
    d2 = cache.get("heygyus-0416", "ResurrectGodAura")
    assert d1 is d2
    assert cache.fetched_count == 1
    assert cache.cache_hit_count == 1
    assert cache.failed_count == 0


def test_deduped_cache_counts_failures_without_raising(tmp_path, sample_payload):
    class FlakyHttp:
        def get_json(self, url):
            raise RuntimeError("simulated network failure")

    fetcher2 = NinjaCharsFetcher(http_client=FlakyHttp(), failed_responses_dir=tmp_path / "failed")
    cache = DedupedCharacterCache(fetcher2, SNAPSHOT)
    result = cache.get("acc", "char")
    assert result is None
    assert cache.failed_count == 1
