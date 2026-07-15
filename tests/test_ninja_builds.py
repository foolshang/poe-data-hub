"""Unit tests for src/fetchers/ninja_builds.py against real captured samples.

Samples were captured live from poe.ninja's undocumented builds-search API on
2026-07-08 via a headless-browser network capture (see the module docstring
in src/fetchers/ninja_builds.py for how the endpoints/binary format were
reverse-engineered) — these tests never hit the network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.fetchers.ninja_builds import NinjaBuildsFetcher, SnapshotVersion

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"

SEARCH_RESPONSE_PATH = SAMPLES / "ninja_poe2_builds_search_ring2.json"
INDEX_STATE_PATH = SAMPLES / "ninja_poe2_index_state.json"

DICT_FILES = {
    "d2de3687082b639617161bd69929688d39768eb7": SAMPLES / "ninja_poe2_builds_dict1.json",
    "37249244431ff0d62d38570a6053e781c35ae84b": SAMPLES / "ninja_poe2_dict_37249244431ff0d62d38570a6053e781c35ae84b.json",
}


class _FakeHttpClient:
    def __init__(self, json_responses=None, bytes_responses=None):
        self._json = json_responses or {}
        self._bytes = bytes_responses or {}
        self.bytes_calls: list[str] = []

    def get_json(self, url):
        return self._json[url]

    def get_bytes(self, url):
        self.bytes_calls.append(url)
        base = url.split("?")[0]
        for key, path in self._bytes.items():
            if base.endswith(key):
                return path.read_bytes()
        raise AssertionError(f"unexpected get_bytes call: {url}")


@pytest.fixture
def fetcher(tmp_path):
    import json

    index_state = json.loads(INDEX_STATE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(
        json_responses={"https://poe.ninja/poe2/api/data/index-state": index_state},
        bytes_responses={
            "builds/0848-20260708-30655/search": SEARCH_RESPONSE_PATH,
            "builds/dictionary/d2de3687082b639617161bd69929688d39768eb7": DICT_FILES[
                "d2de3687082b639617161bd69929688d39768eb7"
            ],
            "builds/dictionary/37249244431ff0d62d38570a6053e781c35ae84b": DICT_FILES[
                "37249244431ff0d62d38570a6053e781c35ae84b"
            ],
        },
    )
    return NinjaBuildsFetcher(
        http_client=http,
        failed_responses_dir=tmp_path / "failed",
        last_good_sample_path=tmp_path / "last_good_builds.bin",
    )


def test_resolve_snapshot_finds_current_league(fetcher: NinjaBuildsFetcher):
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    assert snap == SnapshotVersion(
        league_url="runesofaldur",
        league_name="Runes of Aldur",
        version="0848-20260708-30655",
        snapshot_name="runes-of-aldur",
    )


def test_resolve_snapshot_unknown_league_raises(fetcher: NinjaBuildsFetcher):
    with pytest.raises(ValueError, match="no snapshotVersions entry"):
        fetcher.resolve_snapshot("Not A Real League")


def test_fetch_slot_extracts_known_characters(fetcher: NinjaBuildsFetcher):
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    chars = fetcher.fetch_slot(snap, "ring")

    assert len(chars) == 100
    first = chars[0]
    assert first.name == "ResurrectGodAura"
    assert first.account == "heygyus-0416"
    assert first.level == 100
    assert first.char_class == "Martial Artist"
    assert first.main_skill == "Hollow Focus"


def test_fetch_slot_saves_last_good_sample(fetcher: NinjaBuildsFetcher, tmp_path):
    sample_path = tmp_path / "last_good_builds.bin"
    assert not sample_path.exists()
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    fetcher.fetch_slot(snap, "ring")
    assert sample_path.exists()
    assert sample_path.read_bytes() == SEARCH_RESPONSE_PATH.read_bytes()


def test_fetch_slot_unknown_slot_raises(fetcher: NinjaBuildsFetcher):
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    with pytest.raises(ValueError, match="unknown slot"):
        fetcher.fetch_slot(snap, "not-a-slot")


def test_dictionary_is_cached_across_slot_calls(fetcher: NinjaBuildsFetcher):
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    fetcher.fetch_slot(snap, "ring")
    fetcher.fetch_slot(snap, "ring")

    http = fetcher._http
    class_dict_calls = [u for u in http.bytes_calls if "d2de3687" in u]
    assert len(class_dict_calls) == 1


def test_malformed_row_is_skipped_not_raised(fetcher: NinjaBuildsFetcher, monkeypatch):
    snap = fetcher.resolve_snapshot("Runes of Aldur")

    real_extract = NinjaBuildsFetcher._row_int
    calls = {"n": 0}

    def flaky_row_int(row):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated bad row")
        return real_extract(row)

    monkeypatch.setattr(NinjaBuildsFetcher, "_row_int", staticmethod(flaky_row_int))
    chars = fetcher.fetch_slot(snap, "ring")
    assert len(chars) == 99  # one row skipped, no crash


def test_unknown_game_raises_at_construction():
    with pytest.raises(ValueError, match="unknown game"):
        NinjaBuildsFetcher(http_client=_FakeHttpClient(), game="poe3")


# ── phase 10: PoE1 (same protobuf format, different URL prefix + a "type"
# field on snapshotVersions entries that PoE2's lack) ──────────────────────

POE1_INDEX_STATE_PATH = SAMPLES / "ninja_poe1_index_state.json"
POE1_SEARCH_RESPONSE_PATH = SAMPLES / "ninja_poe1_builds_search_ring.json"
POE1_CLASS_DICT_HASH = "6f37ab7e1609c8464c4f6f676a9fa8fb4434fad9"
POE1_GEM_DICT_HASH = "206ed9544882476118a453ce2f7a29a50a46ca70"


@pytest.fixture
def poe1_fetcher(tmp_path):
    import json

    index_state = json.loads(POE1_INDEX_STATE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(
        json_responses={"https://poe.ninja/poe1/api/data/index-state": index_state},
        bytes_responses={
            "builds/1036-20260714-09576/search": POE1_SEARCH_RESPONSE_PATH,
            f"builds/dictionary/{POE1_CLASS_DICT_HASH}": SAMPLES / f"ninja_poe1_dict_{POE1_CLASS_DICT_HASH}.json",
            f"builds/dictionary/{POE1_GEM_DICT_HASH}": SAMPLES / f"ninja_poe1_dict_{POE1_GEM_DICT_HASH}.json",
        },
    )
    return NinjaBuildsFetcher(
        http_client=http, game="poe1",
        failed_responses_dir=tmp_path / "failed",
        last_good_sample_path=tmp_path / "last_good_builds_poe1.bin",
    )


def test_resolve_snapshot_picks_exp_type_not_depthsolo(poe1_fetcher: NinjaBuildsFetcher):
    # PoE1's index-state has TWO "Mirage" entries (type="exp" and
    # type="depthsolo") -- must resolve to the general-population "exp" one.
    snap = poe1_fetcher.resolve_snapshot("Mirage")
    assert snap.version == "1036-20260714-09576"
    assert snap.snapshot_name == "mirage"


def test_poe1_fetch_slot_extracts_known_characters(poe1_fetcher: NinjaBuildsFetcher):
    snap = poe1_fetcher.resolve_snapshot("Mirage")
    chars = poe1_fetcher.fetch_slot(snap, "ring")

    assert len(chars) == 100
    first = chars[0]
    assert first.name == "GarfSkinEnjoyerrr"
    assert first.account == "Poteitik-3151"
    assert first.level == 100
    assert first.char_class == "Pathfinder"


def test_poe1_slot_filters_have_no_focus_or_charm():
    from src.fetchers.ninja_builds import POE1_SLOT_FILTERS

    assert "focus" not in POE1_SLOT_FILTERS
    assert "charm" not in POE1_SLOT_FILTERS


def test_poe1_slot_filters_use_poe1_weapon_types_not_poe2s():
    from src.fetchers.ninja_builds import POE1_SLOT_FILTERS

    weapon_filters = set(POE1_SLOT_FILTERS["weapon"])
    assert "Rare Claw" in weapon_filters
    assert "Rare Dagger" in weapon_filters
    assert "Rare Crossbow" not in weapon_filters  # PoE2-only weapon type
    assert "Rare Sceptre" not in weapon_filters   # PoE2-only weapon type
