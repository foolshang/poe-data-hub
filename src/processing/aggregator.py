"""Aggregate normalized mods into the meta/latest.json mod table (spec 6.3-6.4).

Two counting modes, matching spec 6.3/6.4 exactly:

- Regular slots (ring, amulet, weapon, ...): counted **per character**. The
  denominator for usage_pct is the number of characters in that slot's
  source list (from ninja_builds), not the globally-deduped character
  count -- a character wearing both a rare ring and a rare amulet
  contributes to both the ring list's and the amulet list's denominator
  independently. A stat_id is counted at most once per character even if it
  appears on more than one item in that slot (e.g. both ring slots).

- Jewels: counted **per gem**, not per character (spec 6.4) -- the
  denominator for jewel:<base> is the number of jewels of that base type
  found in the sample, so a character with two Emerald jewels contributes
  two independent gem-instance counts to jewel:emerald.

Every count is also duplicated into an archetype="all" bucket alongside the
character's real archetype, per spec 6.3.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Optional

from src.fetchers.ninja_builds import CharacterRef
from src.fetchers.ninja_chars import CharacterDetail, ItemDetail
from src.processing.archetyper import Archetyper
from src.processing.normalizer import ModNormalizer
from src.processing.skill_normalizer import SkillNormalizer

GetDetail = Callable[[str, str], Optional[CharacterDetail]]
LevelBracket = tuple[int, int]


@dataclass(frozen=True)
class ModCount:
    slot: str
    archetype: str
    stat_id: str
    mod_kind: str
    count: int
    usage_pct: float
    value_min: Optional[float]
    value_max: Optional[float]
    rank_in_slot: int = 0


class _StatBucket:
    __slots__ = ("count", "mod_kind", "value_min", "value_max")

    def __init__(self) -> None:
        self.count = 0
        self.mod_kind = ""
        self.value_min: Optional[float] = None
        self.value_max: Optional[float] = None

    def add(self, mod_kind: str, value_min: Optional[float], value_max: Optional[float]) -> None:
        self.count += 1
        self.mod_kind = mod_kind
        if value_min is not None:
            self.value_min = value_min if self.value_min is None else min(self.value_min, value_min)
        if value_max is not None:
            self.value_max = value_max if self.value_max is None else max(self.value_max, value_max)


def _weapon_base_type(detail: CharacterDetail) -> Optional[str]:
    for item in detail.items:
        if item.slot == "weapon":
            return item.base_type
    return None


def _stat_hits_for_items(items: list[ItemDetail], normalizer: ModNormalizer) -> dict[str, tuple[str, Optional[float], Optional[float]]]:
    """One entry per distinct stat_id found across `items` (a character's
    items in a single slot, or a single jewel's items) -> (mod_kind, value_min, value_max).
    value_min/value_max follow spec 6.3's "first number's min / last number's max" rule."""
    hits: dict[str, tuple[str, Optional[float], Optional[float]]] = {}
    for item in items:
        for mod_kind, texts in (
            ("implicit", item.mods.implicit),
            ("explicit", item.mods.explicit),
            ("fractured", item.mods.fractured),
        ):
            for raw_text in texts:
                for nm in normalizer.normalize(raw_text, mod_kind):
                    v_min = nm.values[0] if nm.values else None
                    v_max = nm.values[-1] if nm.values else None
                    prev = hits.get(nm.stat_id)
                    if prev is None:
                        hits[nm.stat_id] = (nm.mod_kind, v_min, v_max)
                    else:
                        pm, pmin, pmax = prev
                        new_min = pmin if v_min is None else (v_min if pmin is None else min(pmin, v_min))
                        new_max = pmax if v_max is None else (v_max if pmax is None else max(pmax, v_max))
                        hits[nm.stat_id] = (pm, new_min, new_max)
    return hits


def aggregate_slot(
    slot: str,
    char_refs: list[CharacterRef],
    get_detail: GetDetail,
    archetyper: Archetyper,
    normalizer: ModNormalizer,
) -> list[ModCount]:
    denom = len(char_refs)
    if denom == 0:
        return []

    counters: dict[str, dict[str, _StatBucket]] = defaultdict(lambda: defaultdict(_StatBucket))

    for ref in char_refs:
        detail = get_detail(ref.account, ref.name)
        if detail is None:
            continue
        slot_items = [i for i in detail.items if i.slot == slot]
        if not slot_items:
            continue
        archetype = archetyper.classify(detail.archetype_hint, _weapon_base_type(detail))
        for stat_id, (mod_kind, v_min, v_max) in _stat_hits_for_items(slot_items, normalizer).items():
            for target in {archetype, "all"}:
                counters[target][stat_id].add(mod_kind, v_min, v_max)

    return _finalize(slot, denom, counters)


def aggregate_jewels(
    char_refs: list[CharacterRef],
    get_detail: GetDetail,
    archetyper: Archetyper,
    normalizer: ModNormalizer,
) -> tuple[list[ModCount], dict[str, int]]:
    """Returns (mod counts, {jewel:<base>: gem_count}) -- the gem counts are
    also what belongs in meta/latest.json's sample.per_slot (spec 8.2's
    example shows "jewel:emerald": 87 alongside per-character slot counts)."""
    denom_by_base: dict[str, int] = defaultdict(int)
    counters: dict[str, dict[str, dict[str, _StatBucket]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(_StatBucket)))

    for ref in char_refs:
        detail = get_detail(ref.account, ref.name)
        if detail is None:
            continue
        jewel_items = [i for i in detail.items if i.slot.startswith("jewel:")]
        if not jewel_items:
            continue
        archetype = archetyper.classify(detail.archetype_hint, _weapon_base_type(detail))
        for jewel in jewel_items:
            base_slot = jewel.slot
            denom_by_base[base_slot] += 1
            for stat_id, (mod_kind, v_min, v_max) in _stat_hits_for_items([jewel], normalizer).items():
                for target in {archetype, "all"}:
                    counters[base_slot][target][stat_id].add(mod_kind, v_min, v_max)

    results: list[ModCount] = []
    for base_slot, per_archetype in counters.items():
        denom = denom_by_base[base_slot]
        if denom == 0:
            continue
        results.extend(_finalize(base_slot, denom, per_archetype))
    return results, dict(denom_by_base)


def compute_archetype_distribution(
    unique_char_refs: list[CharacterRef],
    get_detail: GetDetail,
    archetyper: Archetyper,
) -> list[dict[str, float]]:
    """Archetype breakdown across the deduped set of characters actually
    fetched this snapshot (spec 8.2's `archetype_distribution`)."""
    counts: dict[str, int] = defaultdict(int)
    total = 0
    for ref in unique_char_refs:
        detail = get_detail(ref.account, ref.name)
        if detail is None:
            continue
        archetype = archetyper.classify(detail.archetype_hint, _weapon_base_type(detail))
        counts[archetype] += 1
        total += 1
    if total == 0:
        return []
    return [
        {"archetype": archetype, "pct": round(count / total * 100, 2)}
        for archetype, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]


def _finalize(slot: str, denom: int, counters: dict[str, dict[str, _StatBucket]]) -> list[ModCount]:
    results: list[ModCount] = []
    for archetype, stat_buckets in counters.items():
        ranked = sorted(stat_buckets.items(), key=lambda kv: kv[1].count, reverse=True)
        for rank, (stat_id, bucket) in enumerate(ranked, start=1):
            results.append(
                ModCount(
                    slot=slot,
                    archetype=archetype,
                    stat_id=stat_id,
                    mod_kind=bucket.mod_kind,
                    count=bucket.count,
                    usage_pct=round(bucket.count / denom * 100, 2),
                    value_min=bucket.value_min,
                    value_max=bucket.value_max,
                    rank_in_slot=rank,
                )
            )
    return results


# ---------------------------------------------------------------------------
# Legacy skill dimension (skill-meta phase 2). The live per-slot skill
# aggregation that used to sit here (fed by a first-gem *guess* at the main
# skill) was removed 2026-09-21 in favour of the skill-centric pipeline
# (src/processing/skill_collect.py + skill_counter.py). What remains is
# offline-only: `bracket_for_level` (shared), `SkillModCount` and
# `reprocess_chars_to_skill_mod_counts` (recompute legacy rows from stored
# char_items for `--reprocess-char-items`; those rows are marked
# `collection_method = 'legacy_slot_top100'` and their skill is unreliable).
# ---------------------------------------------------------------------------


def bracket_for_level(level: Optional[int], brackets: list[LevelBracket]) -> Optional[str]:
    """`brackets` is an ordered list of (lo, hi) tuples, e.g. [(28, 65), (65,
    75), (75, 90), (90, 100)]. Each bracket is half-open [lo, hi) except the
    last, which is closed [lo, hi] so a level-100 character lands somewhere
    (there is no bracket starting at 100 to catch it otherwise). A level
    below the first bracket's `lo` (e.g. a fresh level-1 character) matches
    no bracket and returns `None` -- callers must skip that character for
    the skill dimension entirely (they still count normally in the existing
    archetype-based pipeline, untouched)."""
    if level is None:
        return None
    for i, (lo, hi) in enumerate(brackets):
        is_last = i == len(brackets) - 1
        if lo <= level < hi or (is_last and level == hi):
            return f"{lo}-{hi}"
    return None


@dataclass(frozen=True)
class SkillModCount:
    slot: str
    main_skill: str
    level_bracket: str
    archetype: str
    stat_id: str
    mod_kind: str
    count: int
    population: int
    usage_pct: float
    value_min: Optional[float]
    value_max: Optional[float]
    rank_in_bracket: int = 0


def reprocess_chars_to_skill_mod_counts(
    chars: list[tuple[Optional[str], int, list[ItemDetail]]],
    normalizer: ModNormalizer,
    skill_normalizer: SkillNormalizer,
    archetyper: Archetyper,
    level_brackets: list[LevelBracket],
) -> list[SkillModCount]:
    """Recomputes one snapshot's skill-dimension mod counts directly from
    already-stored item data (`src/main.py`'s `run_reprocess_char_items`,
    item 13) -- `chars` is `[(raw_main_skill, level, items), ...]`, one entry
    per `char_items` row. No `CharacterRef`/`get_detail` needed, unlike the
    live `aggregate_slot_by_skill`/`aggregate_jewels_by_skill`, since every
    character's full item list is already in hand; entirely offline, no
    poe.ninja calls."""
    denom: dict[tuple[str, str, str, str], int] = defaultdict(int)
    counters: dict[tuple[str, str, str, str], dict[str, _StatBucket]] = defaultdict(lambda: defaultdict(_StatBucket))

    for main_skill_raw, level, items in chars:
        bracket = bracket_for_level(level, level_brackets)
        if bracket is None:
            continue
        skill = skill_normalizer.normalize(main_skill_raw)
        weapon_base = next((i.base_type for i in items if i.slot == "weapon"), None)
        archetype = archetyper.classify(main_skill_raw, weapon_base)

        by_slot: dict[str, list[ItemDetail]] = defaultdict(list)
        for item in items:
            by_slot[item.slot].append(item)

        for slot, slot_items in by_slot.items():
            key = (slot, skill, bracket, archetype)
            if slot.startswith("jewel:"):
                denom[key] += len(slot_items)  # per-gem-instance (spec 6.4)
                for item in slot_items:
                    for stat_id, (mod_kind, v_min, v_max) in _stat_hits_for_items([item], normalizer).items():
                        counters[key][stat_id].add(mod_kind, v_min, v_max)
            else:
                denom[key] += 1  # per-character
                for stat_id, (mod_kind, v_min, v_max) in _stat_hits_for_items(slot_items, normalizer).items():
                    counters[key][stat_id].add(mod_kind, v_min, v_max)

    return _finalize_skill(denom, counters)


def _finalize_skill(
    denom: dict[tuple[str, str, str, str], int],
    counters: dict[tuple[str, str, str, str], dict[str, _StatBucket]],
) -> list[SkillModCount]:
    results: list[SkillModCount] = []
    for key, stat_buckets in counters.items():
        slot, skill, bracket, archetype = key
        d = denom[key]
        if d == 0:
            continue
        ranked = sorted(stat_buckets.items(), key=lambda kv: kv[1].count, reverse=True)
        for rank, (stat_id, bucket) in enumerate(ranked, start=1):
            results.append(
                SkillModCount(
                    slot=slot,
                    main_skill=skill,
                    level_bracket=bracket,
                    archetype=archetype,
                    stat_id=stat_id,
                    mod_kind=bucket.mod_kind,
                    count=bucket.count,
                    population=d,
                    usage_pct=round(bucket.count / d * 100, 2),
                    value_min=bucket.value_min,
                    value_max=bucket.value_max,
                    rank_in_bracket=rank,
                )
            )
    return results
