"""Unit tests for src/fetchers/league_detector.py, including the phase 9
Hardcore-variant token-matching (find_hardcore_variant) -- derived from
whatever the main league is called each cycle, never hardcoded."""
from __future__ import annotations

from src.fetchers.league_detector import LeagueDetector, find_hardcore_variant


def test_find_hardcore_variant_poe2_hc_prefix():
    names = ["Runes of Aldur", "HC Runes of Aldur", "Standard", "Hardcore"]
    assert find_hardcore_variant("Runes of Aldur", names) == "HC Runes of Aldur"


def test_find_hardcore_variant_poe1_hardcore_prefix():
    names = ["Mirage", "Hardcore Mirage", "Standard", "Hardcore"]
    assert find_hardcore_variant("Mirage", names) == "Hardcore Mirage"


def test_find_hardcore_variant_rejects_permanent_hardcore_league():
    # "Hardcore" alone (the permanent SC-HC league) must never be mistaken
    # for a challenge league's HC variant -- no tokens remain after
    # stripping the hardcore token, so it can't match any real main league.
    names = ["Mirage", "Hardcore"]
    assert find_hardcore_variant("Mirage", names) is None


def test_find_hardcore_variant_does_not_match_a_different_leagues_hc_variant():
    # "Hardcore Ancestors" is the HC variant of the event league "Ancestors",
    # not of main league "Mirage" -- must not be picked.
    names = ["Mirage", "Ancestors", "Hardcore Ancestors"]
    assert find_hardcore_variant("Mirage", names) is None


def test_find_hardcore_variant_no_hc_league_at_all_returns_none():
    names = ["Mirage", "Standard"]
    assert find_hardcore_variant("Mirage", names) is None


def test_find_hardcore_variant_ambiguous_returns_none():
    # Pathological case: two names both reduce to the same tokens as main
    # after stripping a hardcore token -- refuse to guess.
    names = ["Mirage", "HC Mirage", "Hardcore Mirage"]
    result = find_hardcore_variant("Mirage", names)
    assert result is None


class _FakeHttpClient:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, url):
        return self.payload


def test_detect_includes_hardcore_field_end_to_end():
    payload = [
        {"id": "Runes of Aldur"},
        {"id": "HC Runes of Aldur"},
        {"id": "Standard"},
        {"id": "Hardcore"},
    ]
    http = _FakeHttpClient(payload)
    detector = LeagueDetector(http, game="poe2", cache_ttl_seconds=1)
    result = detector.detect()
    assert result.main == "Runes of Aldur"
    assert result.events == []
    assert result.hardcore == "HC Runes of Aldur"


def test_detect_hardcore_is_none_when_no_variant_exists():
    payload = [{"id": "Runes of Aldur"}, {"id": "Standard"}]
    http = _FakeHttpClient(payload)
    detector = LeagueDetector(http, game="poe2", cache_ttl_seconds=1)
    result = detector.detect()
    assert result.hardcore is None


def test_detect_hardcore_with_event_league_present():
    payload = [
        {"id": "Ancestors"},
        {"id": "Mirage"},
        {"id": "Hardcore Mirage"},
        {"id": "Hardcore Ancestors"},
        {"id": "Standard"},
        {"id": "Hardcore"},
    ]
    http = _FakeHttpClient(payload)
    detector = LeagueDetector(http, game="poe1", league_override="Mirage", cache_ttl_seconds=1)
    result = detector.detect()
    assert result.main == "Mirage"
    assert set(result.events) == {"Ancestors"}
    assert result.hardcore == "Hardcore Mirage"  # not "Hardcore Ancestors"


def test_fetch_failure_with_override_falls_back_with_hardcore_none():
    class _FailingHttp:
        def get_json(self, url):
            raise RuntimeError("boom")

    detector = LeagueDetector(_FailingHttp(), game="poe1", league_override="Mirage", cache_ttl_seconds=1)
    result = detector.detect()
    assert result.main == "Mirage"
    assert result.hardcore is None
