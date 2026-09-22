"""Publish-time views over the skill-centric collection's SQLite tables
(`skill_stat_counts` / `skill_passive_counts` / `skill_populations`, written
by `skill_collect.py`/`skill_counter.py`) -- builds `{game}/skills/index.json`,
one `{game}/skills/{main|spirit}/{slug}.json` per skill, a shared
`{game}/skills/dictionary.json`, and the new `{game}/passives/latest.json`.

Nothing here talks to Firebase; `src/main.py`'s `publish_skill_meta` wires
these into `PricesPublisher` calls, the same separation `archive_export.py`
uses for `build_league_archive` vs `export_league_archive`.

A skill's population (`skill_populations`) is the source of truth for which
(skill_type, skill, level_bracket) groups exist -- a thin skill (2-8
characters, common on release day of a niche skill) still gets an index entry
and a file with its real population shown, per the project owner's ask that
F4 be able to show "how much to trust this number", even when every one of
its mods/passives is filtered out by `min_usage_pct`. Only individual
mod/passive rows are dropped by the threshold, never a whole group.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

SKILLS_INDEX_SCHEMA_VERSION = 1
SKILL_FILE_SCHEMA_VERSION = 1
SKILL_DICTIONARY_SCHEMA_VERSION = 1
# Bumped from the old passives/latest.json's schema_version (2, coincidentally
# shared with prices/meta's own top-level SCHEMA_VERSION constant) -- this is
# a full shape replacement: `main_skill`/`archetype`/`rank_in_bracket` are
# gone, `skill_type` is new, and the source table changed from
# keypassive_counts (legacy per-slot, first-gem-guess skill) to
# skill_passive_counts (skill-centric). See SPEC.md 8.9.
PASSIVES_SCHEMA_VERSION = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def skill_slug(name: str) -> str:
    """"Righteous Fire" -> "righteous-fire" -- safe as a Firebase Storage path
    segment. Same shape as `publisher.sanitize_league_filename`, kept as its
    own function since skill names and league names are unrelated concepts
    that happen to need the same slugification rule."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _unique_slugs(names: list[str]) -> dict[str, str]:
    """Slugifies every name, disambiguating collisions ("Foo!" and "Foo?"
    would otherwise both slug to "foo") by appending -2, -3, ... in the given
    order -- deterministic as long as the caller's order is (callers here
    always pass names pre-sorted by facet_count desc)."""
    used: set[str] = set()
    out: dict[str, str] = {}
    for name in names:
        base = slug = skill_slug(name)
        i = 2
        while slug in used:
            slug = f"{base}-{i}"
            i += 1
        used.add(slug)
        out[name] = slug
    return out


@dataclass(frozen=True)
class SkillGroup:
    skill_type: str
    skill: str
    slug: str
    facet_count: int
    populations: dict[str, int]  # level_bracket -> population
    mods: dict[str, dict[str, list[dict[str, Any]]]]  # level_bracket -> slot -> stat rows
    passives: dict[str, list[dict[str, Any]]]  # level_bracket -> passive rows

    def path(self, game: str) -> str:
        return f"{game}/skills/{self.skill_type}/{self.slug}.json"

    def to_file(self, game: str, league: str, days_since_league_start: Optional[int]) -> dict[str, Any]:
        return {
            "schema_version": SKILL_FILE_SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "game": game,
            "league": league,
            "days_since_league_start": days_since_league_start,
            "skill_type": self.skill_type,
            "skill": self.skill,
            "slug": self.slug,
            "facet_count": self.facet_count,
            "populations": self.populations,
            "mods": self.mods,
            "passives": self.passives,
        }

    def index_entry(self, game: str) -> dict[str, Any]:
        return {
            "skill_type": self.skill_type,
            "skill": self.skill,
            "slug": self.slug,
            "facet_count": self.facet_count,
            "populations": self.populations,
            "path": self.path(game),
        }


def build_skill_groups(
    stat_rows: list[tuple],
    passive_rows: list[tuple],
    population_rows: list[tuple],
    min_usage_pct: float,
) -> list[SkillGroup]:
    """`stat_rows`: (skill_type, skill, level_bracket, slot, stat_id, mod_kind,
    count, population, slot_holders, usage_pct, value_min, value_max).
    `passive_rows`: (skill_type, skill, level_bracket, keypassive, count,
    population, usage_pct). `population_rows`: (skill_type, skill,
    level_bracket, population, facet_count) -- see module docstring for why
    this, not the stat/passive rows, decides which groups exist.

    Returned list is sorted (skill_type, then facet_count desc, then name) --
    the same "biggest skill first" convention `skill_plan.build_plan` uses."""
    facet_count: dict[tuple[str, str], int] = {}
    populations: dict[tuple[str, str], dict[str, int]] = {}
    for skill_type, skill, bracket, population, facet in population_rows:
        key = (skill_type, skill)
        facet_count[key] = facet  # identical on every bracket row of a skill
        populations.setdefault(key, {})[bracket] = population

    mods: dict[tuple[str, str], dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for skill_type, skill, bracket, slot, stat_id, mod_kind, count, population, slot_holders, usage_pct, v_min, v_max in stat_rows:
        if usage_pct < min_usage_pct:
            continue
        mods.setdefault((skill_type, skill), {}).setdefault(bracket, {}).setdefault(slot, []).append({
            "stat_id": stat_id, "mod_kind": mod_kind, "count": count, "population": population,
            "slot_holders": slot_holders, "usage_pct": usage_pct, "value_min": v_min, "value_max": v_max,
        })

    passives: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    for skill_type, skill, bracket, keypassive, count, population, usage_pct in passive_rows:
        if usage_pct < min_usage_pct:
            continue
        passives.setdefault((skill_type, skill), {}).setdefault(bracket, []).append({
            "keypassive": keypassive, "count": count, "population": population, "usage_pct": usage_pct,
        })

    for by_bracket in mods.values():
        for by_slot in by_bracket.values():
            for rows in by_slot.values():
                rows.sort(key=lambda r: -r["count"])
    for by_bracket in passives.values():
        for rows in by_bracket.values():
            rows.sort(key=lambda r: -r["count"])

    types = {skill_type for skill_type, _skill in populations}
    slugs = {
        t: _unique_slugs(sorted((s for (ty, s) in populations if ty == t), key=lambda s: -facet_count[(t, s)]))
        for t in types
    }

    groups = [
        SkillGroup(
            skill_type=skill_type, skill=skill, slug=slugs[skill_type][skill],
            facet_count=facet_count.get((skill_type, skill), 0),
            populations=populations[(skill_type, skill)],
            mods=mods.get((skill_type, skill), {}), passives=passives.get((skill_type, skill), {}),
        )
        for (skill_type, skill) in populations
    ]
    groups.sort(key=lambda g: (g.skill_type, -g.facet_count, g.skill))
    return groups


def build_skills_index(
    game: str, league: str, days_since_league_start: Optional[int], population_total: int, groups: list[SkillGroup],
) -> dict[str, Any]:
    return {
        "schema_version": SKILLS_INDEX_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "game": game,
        "league": league,
        "days_since_league_start": days_since_league_start,
        "population_total": population_total,
        "skills": [g.index_entry(game) for g in groups],
    }


def build_skill_dictionary(game: str, stat_dictionary: list[dict[str, str]]) -> dict[str, Any]:
    """Shared stat_id -> template lookup for every `{game}/skills/*` file
    (same content as `meta/latest.json`'s own `stat_dictionary`). Kept as one
    file rather than duplicated into every skill file: at ~1.5MB (PoE1) it
    would otherwise be repeated in 150+ per-skill files."""
    return {
        "schema_version": SKILL_DICTIONARY_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "game": game,
        "stat_dictionary": stat_dictionary,
    }


def build_skill_passives_schema(
    game: str, league: str, days_since_league_start: Optional[int], groups: list[SkillGroup],
) -> dict[str, Any]:
    """`{game}/passives/latest.json`, schema_version 3 (2026-09-21) -- full
    shape replacement, see module/SPEC.md-8.9 docstrings. Still notable/
    keystone tier only, never the full passive tree (unchanged caveat)."""
    passives = [
        {"skill_type": g.skill_type, "skill": g.skill, "level_bracket": bracket, **row}
        for g in groups for bracket, rows in g.passives.items() for row in rows
    ]
    passives.sort(key=lambda r: (r["skill_type"], r["skill"], r["level_bracket"], -r["count"]))
    return {
        "schema_version": PASSIVES_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "game": game,
        "league": league,
        "days_since_league_start": days_since_league_start,
        "source": "keypassives (notable/keystone tier only, not the full passive tree); "
                  "counted per skill-centric list (SPEC.md 8.10) -- a character only counts "
                  "into the skill(s) it was pulled for, not every skill it happens to run",
        "passives": passives,
    }
