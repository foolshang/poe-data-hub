"""Which poe.ninja skill lists to collect, decided from the population facets.

A run's plan is: every skill (main and, in PoE2, spirit -- separate lists,
same rules) used by at least `min_skill_pct` percent of the *whole league
population*, plus the set of character classes to cut because they are
vanishingly rare (unascended base classes). Everything here is a pure
function of the facet table and the config thresholds -- no I/O -- and the
resulting plan is JSON-serialisable so a resumed run continues the exact plan
it started with (see `skill_collect`).

PoE1 Vaal skills ("Vaal Reap") are separate poe.ninja entries next to their
base skill ("Reap"). They are folded: both lists are fetched and their
characters counted under the base name. A folded skill qualifies on the
*combined* count, and a member list is only fetched when it has at least
`min_member_list_count` characters (a 3-character Vaal list is not worth a
request).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.fetchers.ninja_builds import PopulationFacets

SKILL_TYPE_MAIN = "main"
SKILL_TYPE_SPIRIT = "spirit"

_VAAL_PREFIX = "Vaal "


@dataclass(frozen=True)
class PlannedList:
    skill_type: str
    skill: str          # base name (Vaal variants folded into it)
    list_name: str      # the name poe.ninja is actually queried with
    facet_count: int    # characters poe.ninja reports for this list


@dataclass(frozen=True)
class SkillPlan:
    game: str
    population_total: int
    min_skill_pct: float
    min_ascendancy_pct: float
    lists: tuple[PlannedList, ...]
    excluded_classes: frozenset[str]
    # (skill_type, skill) -> combined facet count over the folded lists
    group_counts: dict[tuple[str, str], int]

    def to_json(self) -> dict[str, Any]:
        return {
            "game": self.game,
            "population_total": self.population_total,
            "min_skill_pct": self.min_skill_pct,
            "min_ascendancy_pct": self.min_ascendancy_pct,
            "lists": [
                {"skill_type": l.skill_type, "skill": l.skill, "list_name": l.list_name, "facet_count": l.facet_count}
                for l in self.lists
            ],
            "excluded_classes": sorted(self.excluded_classes),
            "group_counts": [[t, s, c] for (t, s), c in sorted(self.group_counts.items())],
        }

    @staticmethod
    def from_json(d: dict[str, Any]) -> "SkillPlan":
        return SkillPlan(
            game=d["game"],
            population_total=int(d["population_total"]),
            min_skill_pct=float(d["min_skill_pct"]),
            min_ascendancy_pct=float(d["min_ascendancy_pct"]),
            lists=tuple(PlannedList(**l) for l in d["lists"]),
            excluded_classes=frozenset(d["excluded_classes"]),
            group_counts={(t, s): int(c) for t, s, c in d["group_counts"]},
        )

    def skills(self, skill_type: str) -> list[str]:
        return sorted({s for (t, s) in self.group_counts if t == skill_type})


def base_skill_name(game: str, name: str, fold_vaal: bool = True) -> str:
    """PoE1: "Vaal Reap" -> "Reap". Anything else is its own base."""
    if fold_vaal and game == "poe1" and name.startswith(_VAAL_PREFIX) and len(name) > len(_VAAL_PREFIX):
        return name[len(_VAAL_PREFIX):]
    return name


def build_plan(
    game: str,
    facets: PopulationFacets,
    min_skill_pct: float,
    min_ascendancy_pct: float,
    min_member_list_count: int = 1,
    fold_vaal: bool = True,
) -> SkillPlan:
    """`min_skill_pct` / `min_ascendancy_pct` are percentages (0.1 = 0.1%) of
    `facets.total`."""
    total = facets.total
    if total <= 0:
        raise ValueError(f"skill plan({game}): non-positive population total {total}")

    facet_sets: list[tuple[str, dict[str, int]]] = [(SKILL_TYPE_MAIN, facets.skills)]
    if facets.spirit_skills is not None:
        facet_sets.append((SKILL_TYPE_SPIRIT, facets.spirit_skills))

    lists: list[PlannedList] = []
    group_counts: dict[tuple[str, str], int] = {}
    for skill_type, facet in facet_sets:
        groups: dict[str, list[tuple[str, int]]] = {}
        for name, count in facet.items():
            groups.setdefault(base_skill_name(game, name, fold_vaal), []).append((name, count))
        for base, members in groups.items():
            combined = sum(c for _n, c in members)
            if combined / total * 100 < min_skill_pct:
                continue
            group_counts[(skill_type, base)] = combined
            chosen = [(n, c) for n, c in members if c >= min_member_list_count]
            if not chosen:  # never leave a qualifying skill without any list
                chosen = [max(members, key=lambda m: m[1])]
            lists.extend(PlannedList(skill_type, base, n, c) for n, c in chosen)

    lists.sort(key=lambda l: (-group_counts[(l.skill_type, l.skill)], l.skill_type, l.list_name))
    excluded = frozenset(c for c, n in facets.classes.items() if n / total * 100 < min_ascendancy_pct)
    return SkillPlan(
        game=game, population_total=total, min_skill_pct=min_skill_pct, min_ascendancy_pct=min_ascendancy_pct,
        lists=tuple(lists), excluded_classes=excluded, group_counts=group_counts,
    )
