from __future__ import annotations

import pytest

from src.processing.skill_normalizer import UNKNOWN_SKILL, SkillNormalizer


class _FakeHttpClient:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc

    def get_json(self, url):
        if self._raise_exc:
            raise self._raise_exc
        return self._payload


GEMS_PAYLOAD = {
    "schema_version": 1,
    "generated_at": "2026-09-15T00:00:00Z",
    "gems": [
        {"id": "Fireball", "display_name": "Fireball", "is_support": False},
        {"id": "IceNovaAltX", "display_name": "Ice Nova of Frostbolts", "is_support": False},
        {"id": "SupportAddedFireDamage", "display_name": "Added Fire Damage Support", "is_support": True},
    ],
}


@pytest.fixture
def normalizer() -> SkillNormalizer:
    n = SkillNormalizer(_FakeHttpClient(GEMS_PAYLOAD), game="poe1", gems_url="https://x/poe1/gems.json")
    n.load()
    return n


def test_load_populates_available_and_count(normalizer):
    assert normalizer.available is True
    assert normalizer.loaded_count == 3


def test_normalize_exact_match(normalizer):
    assert normalizer.normalize("Fireball") == "Fireball"


def test_normalize_keeps_transfigured_distinct_from_base(normalizer):
    assert normalizer.normalize("Ice Nova of Frostbolts") == "Ice Nova of Frostbolts"
    assert normalizer.normalize("Ice Nova of Frostbolts") != normalizer.normalize("Ice Nova")


def test_normalize_unmapped_name_becomes_unknown_and_is_logged(normalizer):
    result = normalizer.normalize("Some Brand New Gem")
    assert result == UNKNOWN_SKILL
    assert normalizer.unmapped_names["Some Brand New Gem"] == 1


def test_normalize_none_becomes_unknown_without_logging(normalizer):
    assert normalizer.normalize(None) == UNKNOWN_SKILL
    assert sum(normalizer.unmapped_names.values()) == 0


def test_normalize_support_gem_match_is_treated_as_unmapped(normalizer):
    """A 'main skill' can never legitimately be a support gem -- a match
    like this means upstream picked the wrong socket group, not a real
    main skill (see module docstring)."""
    result = normalizer.normalize("Added Fire Damage Support")
    assert result == UNKNOWN_SKILL
    assert any("Added Fire Damage Support" in k for k in normalizer.unmapped_names)


def test_load_failure_leaves_available_false_and_normalize_degrades_to_unknown():
    n = SkillNormalizer(_FakeHttpClient(raise_exc=RuntimeError("boom")), game="poe2", gems_url="https://x/poe2/gems.json")
    n.load()
    assert n.available is False
    assert n.normalize("Fireball") == UNKNOWN_SKILL


def test_load_rejects_malformed_response():
    n = SkillNormalizer(_FakeHttpClient({"gems": []}), game="poe2", gems_url="https://x/poe2/gems.json")
    n.load()
    assert n.available is False
