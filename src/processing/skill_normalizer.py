"""Normalizes a character's raw `main_skill` string (as reported by
poe.ninja, see `src/fetchers/ninja_chars.py`'s `_extract_main_skill`)
against `{game}/gems.json`'s `display_name` table (skill-meta phase 1,
SPEC.md 8.6) before skill-dimension mod counting.

Exact-string lookup only -- confirmed live 2026-09-15 against a real PoE2
character sample that poe.ninja's skill names match RePoE/gems.json
`display_name` byte-for-byte (including apostrophes and unusual real gem
names like "Hollow Focus"), so no fuzzy matching is needed. Transfigured
variants are already distinct `display_name` entries in gems.json (e.g.
"Ice Nova of Frostbolts" vs base "Ice Nova") -- exact-name lookup keeps
them separate automatically, never collapsed into their base skill.

A matched name whose gems.json entry has `is_support: true` is treated as
unmapped too: a "main skill" can never legitimately be a support gem, so a
match like that means the upstream extraction picked the wrong socket
group for this character, not a real main skill -- counting it as-is would
silently corrupt the skill dimension with a support-gem "skill".

Unmapped names (no match, or a support-gem match) normalize to `"unknown"`
and are always counted under that bucket rather than dropped (spec: never
silently discard a sample), with an occurrence log
(`unmapped_names`, a `Counter`) so a human can see what's missing from
gems.json -- e.g. a brand-new gem RePoE's datamine hasn't picked up yet.

Never crashes the meta job: if `{game}/gems.json` itself can't be loaded
(spec 10.1b), `available` stays `False` and every `normalize()` call
returns `"unknown"` for the run -- degrades the new skill dimension to
all-unknown rather than failing the whole meta job, which still needs its
existing archetype-based counting to succeed regardless.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

UNKNOWN_SKILL = "unknown"


class SkillNormalizer:
    def __init__(self, http_client: HttpClient, game: str, gems_url: str):
        self._http = http_client
        self._game = game
        self._gems_url = gems_url
        self._is_support_by_name: dict[str, bool] = {}
        self.unmapped_names: Counter[str] = Counter()
        self.available = False
        self.loaded_count = 0

    def load(self) -> None:
        try:
            data = self._http.get_json(self._gems_url)
            gems = data.get("gems")
            if not isinstance(gems, list) or not gems:
                raise ValueError(f"unexpected {self._gems_url} shape: no non-empty 'gems' list")
        except Exception as e:
            log.error(
                "skill_normalizer(%s): could not load %s (%s) -- skill dimension will report "
                "'unknown' for every character this run; existing archetype-based counting is unaffected",
                self._game, self._gems_url, e,
            )
            return

        for g in gems:
            name = g.get("display_name")
            if name:
                self._is_support_by_name[name] = bool(g.get("is_support"))
        self.available = True
        self.loaded_count = len(self._is_support_by_name)

    def normalize(self, raw_main_skill: Optional[str]) -> str:
        if not raw_main_skill:
            return UNKNOWN_SKILL
        is_support = self._is_support_by_name.get(raw_main_skill)
        if is_support is None:
            self.unmapped_names[raw_main_skill] += 1
            log.debug("skill_normalizer(%s): unmapped main_skill %r", self._game, raw_main_skill)
            return UNKNOWN_SKILL
        if is_support:
            self.unmapped_names[f"{raw_main_skill} (support gem, not a valid main skill)"] += 1
            log.debug(
                "skill_normalizer(%s): main_skill %r resolved to a support gem in gems.json -- "
                "treating as unmapped rather than a real main skill", self._game, raw_main_skill,
            )
            return UNKNOWN_SKILL
        return raw_main_skill
