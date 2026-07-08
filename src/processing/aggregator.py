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

GetDetail = Callable[[str, str], Optional[CharacterDetail]]


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
        archetype = archetyper.classify(detail.main_skill, _weapon_base_type(detail))
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
        archetype = archetyper.classify(detail.main_skill, _weapon_base_type(detail))
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
        archetype = archetyper.classify(detail.main_skill, _weapon_base_type(detail))
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
