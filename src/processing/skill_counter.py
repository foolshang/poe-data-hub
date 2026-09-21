"""Count mods and passives for one skill group (skill-centric collection).

A skill group's members are the characters poe.ninja returned for that skill's
list(s) (a folded PoE1 Vaal list adds its members to the base skill's group).
Each character is counted **only into the groups it was pulled for** -- never
into every skill it happens to run -- so every group gets a comparable base
(~100 characters) and percentages compare across skills.

Dimensions: (skill_type, skill, level_bracket, slot, stat_id). No archetype
split: the skill already says what kind of build it is, and splitting it again
fragments cells to nothing (2026-09-21 distribution survey: median cell had
1-2 characters).

Counting is per character everywhere, jewels included: a character counts once
for a stat in `jewel:<base>` even if two of its jewels of that base carry it.
`population` is the group's character count in the bracket (the same base for
all of the group's slots and stats); `slot_holders` is how many of those
characters wear at least one item in the slot, kept so a "% of people who use
this slot" reading stays derivable. No count threshold is applied here --
SQLite keeps every stat; publish filtering is a separate, config-driven step.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Optional

from src.fetchers.ninja_chars import CharacterDetail
from src.processing.aggregator import LevelBracket, _StatBucket, _stat_hits_for_items, bracket_for_level
from src.processing.normalizer import ModNormalizer

# (account, name, list-row level) -> stored detail, or None when unavailable
GetStoredDetail = Callable[[str, str], Optional[CharacterDetail]]

# (account, name, list-row level, class, keypassives) as stored per member
Member = tuple[str, str, int, str, list[str]]


@dataclass
class GroupResult:
    stat_rows: list[dict]
    passive_rows: list[dict]
    population_rows: list[dict]
    counted: int
    missing_detail: int
    cut_by_class: int
    no_bracket: int


def count_skill_group(
    skill_type: str,
    skill: str,
    members: list[Member],
    get_detail: GetStoredDetail,
    normalizer: ModNormalizer,
    level_brackets: list[LevelBracket],
    excluded_classes: frozenset[str] | set[str],
    facet_count: int,
) -> GroupResult:
    population: dict[str, int] = defaultdict(int)
    slot_holders: dict[tuple[str, str], int] = defaultdict(int)
    counters: dict[tuple[str, str], dict[str, _StatBucket]] = defaultdict(lambda: defaultdict(_StatBucket))
    passive_counts: dict[tuple[str, str], int] = defaultdict(int)
    counted = missing = cut = no_bracket = 0

    for account, name, row_level, char_class, keypassives in members:
        if char_class in excluded_classes:
            cut += 1
            continue
        detail = get_detail(account, name)
        if detail is None:
            missing += 1
            continue
        bracket = bracket_for_level(detail.level, level_brackets)
        if bracket is None:
            no_bracket += 1
            continue
        counted += 1
        population[bracket] += 1

        by_slot: dict[str, list] = defaultdict(list)
        for item in detail.items:
            by_slot[item.slot].append(item)
        for slot, slot_items in by_slot.items():
            slot_holders[(bracket, slot)] += 1
            for stat_id, (mod_kind, v_min, v_max) in _stat_hits_for_items(slot_items, normalizer).items():
                counters[(bracket, slot)][stat_id].add(mod_kind, v_min, v_max)
        for kp in set(keypassives):
            passive_counts[(bracket, kp)] += 1

    stat_rows: list[dict] = []
    for (bracket, slot), buckets in counters.items():
        pop = population[bracket]
        for stat_id, b in buckets.items():
            stat_rows.append({
                "level_bracket": bracket, "slot": slot, "stat_id": stat_id, "mod_kind": b.mod_kind,
                "count": b.count, "population": pop, "slot_holders": slot_holders[(bracket, slot)],
                "usage_pct": round(b.count / pop * 100, 2), "value_min": b.value_min, "value_max": b.value_max,
            })
    passive_rows = [
        {
            "level_bracket": bracket, "keypassive": kp, "count": n, "population": population[bracket],
            "usage_pct": round(n / population[bracket] * 100, 2),
        }
        for (bracket, kp), n in passive_counts.items()
    ]
    population_rows = [
        {"level_bracket": bracket, "population": pop, "facet_count": facet_count}
        for bracket, pop in population.items()
    ]
    return GroupResult(
        stat_rows=sorted(stat_rows, key=lambda r: (r["level_bracket"], r["slot"], -r["count"], r["stat_id"])),
        passive_rows=sorted(passive_rows, key=lambda r: (r["level_bracket"], -r["count"], r["keypassive"])),
        population_rows=sorted(population_rows, key=lambda r: r["level_bracket"]),
        counted=counted, missing_detail=missing, cut_by_class=cut, no_bracket=no_bracket,
    )
