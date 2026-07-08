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
