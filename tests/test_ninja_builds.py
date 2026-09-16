"""Unit tests for src/fetchers/ninja_builds.py against real captured samples.

Samples captured live from poe.ninja's undocumented builds-search API on
2026-09-16 via direct `curl`/browser fetches (confirmed byte-identical --
see the module docstring in src/fetchers/ninja_builds.py) after poe.ninja's
2026-08-04 wire-format change was diagnosed. The pre-2026-09-16 samples this
file used to reference (captured 2026-07-08/07-14, old field1/field5/field6
schema) are obsolete and were removed along with the old parser -- these
tests never hit the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.ninja_builds import NinjaBuildsFetcher, SnapshotVersion

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"

SEARCH_RESPONSE_PATH = SAMPLES / "ninja_poe2_builds_search_2026-09-16.bin"
INDEX_STATE_PATH = SAMPLES / "ninja_poe2_index_state_2026-09-16.json"
CLASS_DICT_HASH = "57ae44d678bf18b690ba390205e346428fc51e72"
GEM_DICT_HASH = "82cc7dced089486135a9489463036480a12ac6ad"

DICT_FILES = {
    CLASS_DICT_HASH: SAMPLES / "ninja_poe2_dict_class_2026-09-16.bin",
    GEM_DICT_HASH: SAMPLES / "ninja_poe2_dict_gem_2026-09-16.bin",
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
    index_state = json.loads(INDEX_STATE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(
        json_responses={"https://poe.ninja/poe2/api/data/index-state": index_state},
        bytes_responses={
            "builds/0336-20260916-24709/search": SEARCH_RESPONSE_PATH,
            f"builds/dictionary/{CLASS_DICT_HASH}": DICT_FILES[CLASS_DICT_HASH],
            f"builds/dictionary/{GEM_DICT_HASH}": DICT_FILES[GEM_DICT_HASH],
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
        version="0336-20260916-24709",
        snapshot_name="runes-of-aldur",
    )


def test_resolve_snapshot_unknown_league_raises(fetcher: NinjaBuildsFetcher):
    with pytest.raises(ValueError, match="no snapshotVersions entry"):
        fetcher.resolve_snapshot("Not A Real League")


def test_resolve_snapshot_saves_last_good_index_state(fetcher: NinjaBuildsFetcher, tmp_path):
    path = tmp_path / "last_good_index_state_poe2.json"
    assert not path.exists()
    fetcher.resolve_snapshot("Runes of Aldur")
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["snapshotVersions"]


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


def test_fetch_slot_saves_last_good_dictionaries(fetcher: NinjaBuildsFetcher, tmp_path):
    class_path = tmp_path / "last_good_dict_poe2_class.bin"
    gem_path = tmp_path / "last_good_dict_poe2_gem.bin"
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    fetcher.fetch_slot(snap, "ring")
    assert class_path.read_bytes() == DICT_FILES[CLASS_DICT_HASH].read_bytes()
    assert gem_path.read_bytes() == DICT_FILES[GEM_DICT_HASH].read_bytes()


def test_fetch_slot_unknown_slot_raises(fetcher: NinjaBuildsFetcher):
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    with pytest.raises(ValueError, match="unknown slot"):
        fetcher.fetch_slot(snap, "not-a-slot")


def test_dictionary_is_cached_across_slot_calls(fetcher: NinjaBuildsFetcher):
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    fetcher.fetch_slot(snap, "ring")
    fetcher.fetch_slot(snap, "ring")

    http = fetcher._http
    class_dict_calls = [u for u in http.bytes_calls if CLASS_DICT_HASH in u]
    assert len(class_dict_calls) == 1


def test_repeat_fetch_of_same_filter_does_not_re_request(fetcher: NinjaBuildsFetcher):
    """Etiquette: poe.ninja is a free community site -- cache results, never
    re-fetch the same (snapshot, item_filter) within one fetcher instance."""
    snap = fetcher.resolve_snapshot("Runes of Aldur")
    fetcher.fetch_slot(snap, "ring")
    fetcher.fetch_slot(snap, "ring")

    http = fetcher._http
    search_calls = [u for u in http.bytes_calls if "search" in u]
    assert len(search_calls) == 1


def test_malformed_row_is_skipped_not_raised(fetcher: NinjaBuildsFetcher, monkeypatch):
    snap = fetcher.resolve_snapshot("Runes of Aldur")

    real_main_skill = NinjaBuildsFetcher._main_skill
    calls = {"n": 0}

    def flaky_main_skill(skills_col, i, gem_dict):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated bad row")
        return real_main_skill(skills_col, i, gem_dict)

    monkeypatch.setattr(NinjaBuildsFetcher, "_main_skill", staticmethod(flaky_main_skill))
    chars = fetcher.fetch_slot(snap, "ring")
    assert len(chars) == 99  # one row skipped, no crash


def test_unknown_game_raises_at_construction():
    with pytest.raises(ValueError, match="unknown game"):
        NinjaBuildsFetcher(http_client=_FakeHttpClient(), game="poe3")


# ── requirement (1): a schema mismatch must fail loudly, never silently
# return an empty result -- this is exactly how the real 2026-08-04 -> 09-16
# incident presented (see module docstring) ─────────────────────────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_num: int, wire_type: int) -> bytes:
    return _varint((field_num << 3) | wire_type)


def _build_search_body(total: int, column_labels: list[str]) -> bytes:
    """Hand-build a minimal protobuf search-response body: field 1 = total
    (varint), plus one empty (no field 6/7/9 rows) field-12 valueList entry
    per label in `column_labels` -- simulating the exact shape of the real
    2026-08-04 broken responses (nonzero total, no usable row data)."""
    body = _tag(1, 0) + _varint(total)
    for label in column_labels:
        label_bytes = label.encode("utf-8")
        entry = _tag(1, 2) + _varint(len(label_bytes)) + label_bytes
        body += _tag(12, 2) + _varint(len(entry)) + entry
    return body


def _wrap_top_level(body: bytes) -> bytes:
    return _tag(1, 2) + _varint(len(body)) + body


def test_zero_rows_with_nonzero_total_raises_not_silently_empty(fetcher: NinjaBuildsFetcher, tmp_path):
    body = _build_search_body(total=117827, column_labels=["name", "account", "level", "class"])
    wrapped = _wrap_top_level(body)
    fetcher._http._bytes["builds/0336-20260916-24709/search"] = tmp_path / "synthetic.bin"
    (tmp_path / "synthetic.bin").write_bytes(wrapped)

    snap = fetcher.resolve_snapshot("Runes of Aldur")
    with pytest.raises(ValueError, match="schema mismatch"):
        fetcher.fetch_slot(snap, "ring")


def test_zero_rows_with_zero_total_is_a_genuine_empty_result(fetcher: NinjaBuildsFetcher, tmp_path, caplog):
    body = _build_search_body(total=0, column_labels=["name", "account", "level", "class"])
    wrapped = _wrap_top_level(body)
    fetcher._http._bytes["builds/0336-20260916-24709/search"] = tmp_path / "synthetic_empty.bin"
    (tmp_path / "synthetic_empty.bin").write_bytes(wrapped)

    snap = fetcher.resolve_snapshot("Runes of Aldur")
    with caplog.at_level("WARNING"):
        chars = fetcher.fetch_slot(snap, "ring")
    assert chars == []
    assert any("genuinely empty" in r.message for r in caplog.records)


def test_missing_required_columns_raises_with_found_columns_listed(fetcher: NinjaBuildsFetcher, tmp_path):
    body = _build_search_body(total=50, column_labels=["totallydifferentcolumn"])
    wrapped = _wrap_top_level(body)
    fetcher._http._bytes["builds/0336-20260916-24709/search"] = tmp_path / "synthetic_missing.bin"
    (tmp_path / "synthetic_missing.bin").write_bytes(wrapped)

    snap = fetcher.resolve_snapshot("Runes of Aldur")
    with pytest.raises(ValueError, match=r"totallydifferentcolumn"):
        fetcher.fetch_slot(snap, "ring")


def test_indexed_flag_is_logged_but_never_gates_a_query(fetcher: NinjaBuildsFetcher, caplog):
    """Requirement (2): buildLeagues[].indexed is informational only --
    confirmed live 2026-09-16 it doesn't predict data availability (PoE2's
    real main league reports indexed=false yet returns full data). Must
    never raise/skip based on this flag."""
    with caplog.at_level("INFO"):
        snap = fetcher.resolve_snapshot("Runes of Aldur")
    fetcher.fetch_slot(snap, "ring")  # must not raise/skip
    assert any("buildLeagues.indexed" in r.message for r in caplog.records)


# ── phase 10: PoE1 (same current schema, different URL prefix + a "type"
# field on snapshotVersions entries that PoE2's lack) ──────────────────────

POE1_INDEX_STATE_PATH = SAMPLES / "ninja_poe1_index_state_2026-09-16.json"
POE1_SEARCH_RESPONSE_PATH = SAMPLES / "ninja_poe1_builds_search_2026-09-16.bin"
POE1_CLASS_DICT_HASH = "1f8a7450649b94a3e6895acbd056902cad1bd353"
POE1_GEM_DICT_HASH = "0afdf1bad8cbab5592544c9314e5312737327528"


@pytest.fixture
def poe1_fetcher(tmp_path):
    index_state = json.loads(POE1_INDEX_STATE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(
        json_responses={"https://poe.ninja/poe1/api/data/index-state": index_state},
        bytes_responses={
            "builds/0346-20260916-12666/search": POE1_SEARCH_RESPONSE_PATH,
            f"builds/dictionary/{POE1_CLASS_DICT_HASH}": SAMPLES / "ninja_poe1_dict_class_2026-09-16.bin",
            f"builds/dictionary/{POE1_GEM_DICT_HASH}": SAMPLES / "ninja_poe1_dict_gem_2026-09-16.bin",
        },
    )
    return NinjaBuildsFetcher(
        http_client=http, game="poe1",
        failed_responses_dir=tmp_path / "failed",
        last_good_sample_path=tmp_path / "last_good_builds_poe1.bin",
    )


def test_resolve_snapshot_picks_exp_type_not_depthsolo(poe1_fetcher: NinjaBuildsFetcher):
    # PoE1's index-state has TWO "Allflame" entries (type="exp" and
    # type="depthsolo") -- must resolve to the general-population "exp" one.
    snap = poe1_fetcher.resolve_snapshot("Allflame")
    assert snap.version == "0346-20260916-12666"
    assert snap.snapshot_name == "allflame"


def test_poe1_fetch_slot_extracts_known_characters(poe1_fetcher: NinjaBuildsFetcher):
    snap = poe1_fetcher.resolve_snapshot("Allflame")
    chars = poe1_fetcher.fetch_slot(snap, "ring")

    assert len(chars) == 100
    first = chars[0]
    assert first.name == "ПОТЕЙТИК"  # "ПОТЕЙТИК"
    assert first.account == "Poteitik-3151"
    assert first.level == 100
    assert first.char_class == "Champion"
    assert first.main_skill == "Desecrate"


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
