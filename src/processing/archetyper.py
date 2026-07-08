"""Classify a character into an archetype from its main skill gem and
equipped weapon (spec 7.2). Rules live in config/archetype_rules.yaml so
they're tunable without a code change.

Classification is necessarily approximate — a gem name alone doesn't always
say whether a skill is a spell or a weapon attack, or what element it deals.
Unclassifiable characters get "unknown" and are still counted in the "all"
archetype bucket downstream (spec 6.3/6.5's stated accuracy caveat).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "archetype_rules.yaml"

_ELEMENT_ORDER = ("fire", "cold", "lightning", "chaos", "physical")


class Archetyper:
    def __init__(self, rules_path: Path = DEFAULT_RULES_PATH):
        with rules_path.open("r", encoding="utf-8") as f:
            self._rules: dict[str, Any] = yaml.safe_load(f) or {}

    def classify(self, main_skill: Optional[str], weapon_base_type: Optional[str]) -> str:
        if not main_skill:
            return "unknown"
        skill_lower = main_skill.lower()

        if self._matches_any(skill_lower, self._rules.get("minion_keywords", [])):
            return "minion"

        if self._matches_any(skill_lower, self._rules.get("totem_trap_mine_keywords", [])):
            return "totem-trap-mine"

        element_keywords = self._rules.get("element_keywords", {})
        for element in _ELEMENT_ORDER:
            if self._matches_any(skill_lower, element_keywords.get(element, [])):
                return f"spell-{element}"

        return self._weapon_attack_fallback(weapon_base_type)

    def _weapon_attack_fallback(self, weapon_base_type: Optional[str]) -> str:
        fallback_rules = self._rules.get("weapon_attack_fallback", {})
        if not weapon_base_type:
            return "unknown"
        # Longest keyword first so e.g. "Crossbow" is checked before "Bow"
        # (which is a substring of it).
        specific_rules = sorted(
            (kv for kv in fallback_rules.items() if kv[0] != "default"),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )
        for key, archetype in specific_rules:
            if key.lower() in weapon_base_type.lower():
                return archetype
        return fallback_rules.get("default", "unknown")

    @staticmethod
    def _matches_any(haystack_lower: str, keywords: list[str]) -> bool:
        return any(kw.lower() in haystack_lower for kw in keywords)
