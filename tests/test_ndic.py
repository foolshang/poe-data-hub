"""Unit tests for src/utils/ndic.py against real captured poe.ninja
dictionary samples (both games, captured 2026-09-16 -- see
src/fetchers/ninja_builds.py's module docstring)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.utils import ndic

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


def test_decodes_real_poe2_class_dictionary():
    raw = (SAMPLES / "ninja_poe2_dict_class_2026-09-16.bin").read_bytes()
    names = ndic.decode(raw)
    assert len(names) == 30
    assert "Warrior" in names
    assert "Sorceress" in names
    assert "Deadeye" in names


def test_decodes_real_poe2_gem_dictionary():
    raw = (SAMPLES / "ninja_poe2_dict_gem_2026-09-16.bin").read_bytes()
    names = ndic.decode(raw)
    assert len(names) == 1280
    assert names[576] == "Hollow Focus"
    assert "Herald of Ice" in names


def test_decodes_real_poe1_class_dictionary():
    raw = (SAMPLES / "ninja_poe1_dict_class_2026-09-16.bin").read_bytes()
    names = ndic.decode(raw)
    assert "Champion" in names
    assert "Elementalist" in names


def test_decodes_real_poe1_gem_dictionary():
    raw = (SAMPLES / "ninja_poe1_dict_gem_2026-09-16.bin").read_bytes()
    names = ndic.decode(raw)
    assert "Desecrate" in names


def test_bad_magic_raises_immediately_with_what_was_found():
    with pytest.raises(ndic.NdicParseError, match=r"b'ABCD'"):
        ndic.decode(b"ABCD" + b"\x00" * 20)


def test_wrong_version_raises_immediately_with_version_found():
    raw = b"NDIC" + (99).to_bytes(4, "little") + b"\x00" * 20
    with pytest.raises(ndic.NdicParseError, match="got 99"):
        ndic.decode(raw)


def test_truncated_header_raises():
    with pytest.raises(ndic.NdicParseError, match="truncated header"):
        ndic.decode(b"NDIC\x02\x00\x00\x00")


def test_zero_count_returns_empty_list():
    raw = b"NDIC" + (2).to_bytes(4, "little") + b"\x00\x00\x00\x00" + (0).to_bytes(4, "little")
    assert ndic.decode(raw) == []


# --- 2026-09-21 regression: ambiguous split point -------------------------
# PoE2's class dictionary grew to 31 entries ("Druid") and a second split
# point that also passes the sum-of-lengths check appeared before the real one.
# The old first-match decoder silently returned 31 garbled strings.

def _assert_clean(names):
    assert all(n and all(ord(ch) >= 32 for ch in n) for n in names), names


def test_decodes_2026_09_21_poe2_class_dictionary_with_druid():
    raw = (SAMPLES / "ninja_poe2_dict_class_2026-09-21.bin").read_bytes()
    names = ndic.decode(raw)
    assert len(names) == 31
    _assert_clean(names)
    assert "Druid" in names
    assert "Gemling Legionnaire" in names
    assert names == sorted(names)  # poe.ninja lists them alphabetically


def test_2026_09_21_class_dictionary_really_is_ambiguous_for_a_first_match_decoder():
    """Guards the guard: if this stops being ambiguous the fix above is no
    longer being exercised by real data."""
    raw = (SAMPLES / "ninja_poe2_dict_class_2026-09-21.bin").read_bytes()
    count = int.from_bytes(raw[12:16], "little")
    total = len(raw)
    fits = [
        ts for ts in range(count, total + 1)
        if sum(raw[ts - count:ts]) == total - ts
    ]
    assert len(fits) >= 2


@pytest.mark.parametrize("game,kind,expected_count", [
    ("poe1", "class", 28), ("poe1", "gem", 822), ("poe2", "gem", 1270),
])
def test_decodes_other_2026_09_21_dictionaries_unchanged(game, kind, expected_count):
    names = ndic.decode((SAMPLES / f"ninja_{game}_dict_{kind}_2026-09-21.bin").read_bytes())
    assert len(names) == expected_count
    _assert_clean(names)
