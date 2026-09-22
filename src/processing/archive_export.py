"""Per-league archive export (skill-meta phase 5).

Exports the *entire* accumulated meta history for one (game, league) into a
single file, `data/archive/{game}-{league-slug}.json` -- not a rough
summary. Reads `mod_counts`/`skill_mod_counts` (kept forever across
leagues, never cleaned up) rather than `char_items` (90-day retention), so
a league that ended long before the 90-day window can still be archived in
full -- the whole point of an archive is answering questions next year that
nobody thought to ask today, which requires keeping every dimension
(skill/slot/level_bracket/stat_id), not a pre-aggregated top-N.

The only dimension this collapses is time: every snapshot ever recorded for
the league gets bucketed by `days_since_league_start` into three age
buckets (`"1-21"`, `"22-60"`, `"61+"`) plus `"unknown"` for the rare
snapshot where league-start detection failed and `days_since_league_start`
is NULL -- never silently dropped. `mod_counts` (archetype dimension) and
`skill_mod_counts` (skill+level_bracket dimension, `archetype` intentionally
NOT kept as a separate archive dimension -- it's a fallback-tier field on
that table, not a first-class one, see local_db.py) are aggregated
separately using the same population-dedup logic as
`LocalDb.query_skill_mod_counts`'s `_aggregate_rows`: a (snapshot_id, slot,
skill, level_bracket, archetype) group's `population` is counted once, not
once per stat_id row in that group.

`mod_counts` has no `population` column (unlike `skill_mod_counts`) -- that
denominator only ever lived in the ephemeral published meta.json's
`per_slot_sample`, never persisted per-row. Archived `mod_counts` rows
therefore carry only `count`/`value_min`/`value_max`, deliberately no
`usage_pct` -- computing one by summing counts across snapshots with no
matching population would look precise while being wrong.

`passives` (skill-meta phase 3, path A, shipped 2026-09-17): notable/keystone
passives only, from `keypassive_counts` (same population-dedup treatment as
`skill_mod_counts`, `archetype` dropped as an archive dimension for the same
reason). **Not the full passive tree** -- poe.ninja's own "keypassives"
dictionary only covers notable/keystone-tier passives (confirmed live,
~260-330 entries), not the ~1000+ small stat nodes; see
`src/fetchers/ninja_builds.py`'s module docstring and CLAUDE.md for why a
full-tree "path B" would need real GGG tree data this project doesn't have
yet. For a league with no keypassive_counts rows at all (never reprocessed
under phase 3, or genuinely empty), this is `{}`, not `null` -- `null` only
ever meant "phase 3 doesn't exist yet", which is no longer true.

`skills` (skill-meta phase 22, added 2026-09-21): the skill-centric
collection's own archive, from `skill_stat_counts`/`skill_passive_counts`/
`skill_populations` -- **only ever reads `skill_top100_v1` snapshots**
(`LocalDb.iter_skill_stat_counts_for_league`/`iter_skill_passive_counts_for_league`
filter on `collection_method`), never the `legacy_slot_top100` snapshots that
back `mod_counts`/`skill_mod_counts`/`passives` above. This is an orthogonal
top-level key, not a replacement -- `mod_counts` (archetype dimension, no
main-skill guess involved) keeps spanning every snapshot regardless of
collection method, and `skill_mod_counts`/`passives` keep whatever
`legacy_slot_top100` history already existed. See SPEC.md 8.10.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.storage.local_db import LocalDb
from src.storage.publisher import sanitize_league_filename

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_ARCHIVE_DIR = Path("data/archive")

_AGE_BUCKET_BOUNDS = [("1-21", 1, 21), ("22-60", 22, 60), ("61+", 61, None)]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_bucket(days_since_league_start: Optional[int]) -> str:
    if days_since_league_start is None or days_since_league_start < 1:
        return "unknown"
    for label, lo, hi in _AGE_BUCKET_BOUNDS:
        if hi is None or days_since_league_start <= hi:
            if days_since_league_start >= lo:
                return label
    return "unknown"  # unreachable given the bounds above, but never crash


def _build_skill_mod_archive(rows: list[tuple]) -> dict[str, Any]:
    seen_groups: set[tuple] = set()
    population_by_key: dict[tuple, int] = defaultdict(int)
    stat_by_key: dict[tuple, dict[tuple, dict[str, Any]]] = {}

    for slot, skill, bracket, archetype, stat_id, mod_kind, count, population, v_min, v_max, snapshot_id, days in rows:
        bucket = _age_bucket(days)
        key = (slot, skill, bracket, bucket)
        group_key = (snapshot_id, slot, skill, bracket, archetype)
        if group_key not in seen_groups:
            seen_groups.add(group_key)
            population_by_key[key] += population
        entry = stat_by_key.setdefault(key, {}).setdefault(
            (stat_id, mod_kind), {"stat_id": stat_id, "mod_kind": mod_kind, "count": 0, "value_min": None, "value_max": None},
        )
        entry["count"] += count
        if v_min is not None:
            entry["value_min"] = v_min if entry["value_min"] is None else min(entry["value_min"], v_min)
        if v_max is not None:
            entry["value_max"] = v_max if entry["value_max"] is None else max(entry["value_max"], v_max)

    out: dict[str, Any] = {}
    for (slot, skill, bracket, bucket), stat_map in stat_by_key.items():
        population = population_by_key[(slot, skill, bracket, bucket)]
        stat_rows = sorted(stat_map.values(), key=lambda r: r["count"], reverse=True)
        for r in stat_rows:
            r["population"] = population
            r["usage_pct"] = round(r["count"] / population * 100, 2) if population else 0.0
        out.setdefault(slot, {}).setdefault(skill, {}).setdefault(bracket, {})[bucket] = stat_rows
    return out


def _build_mod_archive(rows: list[tuple]) -> dict[str, Any]:
    stat_by_key: dict[tuple, dict[tuple, dict[str, Any]]] = {}

    for slot, archetype, stat_id, mod_kind, count, v_min, v_max, days in rows:
        bucket = _age_bucket(days)
        key = (slot, archetype, bucket)
        entry = stat_by_key.setdefault(key, {}).setdefault(
            (stat_id, mod_kind), {"stat_id": stat_id, "mod_kind": mod_kind, "count": 0, "value_min": None, "value_max": None},
        )
        entry["count"] += count
        if v_min is not None:
            entry["value_min"] = v_min if entry["value_min"] is None else min(entry["value_min"], v_min)
        if v_max is not None:
            entry["value_max"] = v_max if entry["value_max"] is None else max(entry["value_max"], v_max)

    out: dict[str, Any] = {}
    for (slot, archetype, bucket), stat_map in stat_by_key.items():
        stat_rows = sorted(stat_map.values(), key=lambda r: r["count"], reverse=True)
        out.setdefault(slot, {}).setdefault(archetype, {})[bucket] = stat_rows
    return out


def _build_passive_archive(rows: list[tuple]) -> dict[str, Any]:
    seen_groups: set[tuple] = set()
    population_by_key: dict[tuple, int] = defaultdict(int)
    kp_by_key: dict[tuple, dict[str, dict[str, Any]]] = {}

    for skill, bracket, archetype, keypassive, count, population, snapshot_id, days in rows:
        bucket = _age_bucket(days)
        key = (skill, bracket, bucket)
        group_key = (snapshot_id, skill, bracket, archetype)
        if group_key not in seen_groups:
            seen_groups.add(group_key)
            population_by_key[key] += population
        entry = kp_by_key.setdefault(key, {}).setdefault(
            keypassive, {"keypassive": keypassive, "count": 0},
        )
        entry["count"] += count

    out: dict[str, Any] = {}
    for (skill, bracket, bucket), kp_map in kp_by_key.items():
        population = population_by_key[(skill, bracket, bucket)]
        kp_rows = sorted(kp_map.values(), key=lambda r: r["count"], reverse=True)
        for r in kp_rows:
            r["population"] = population
            r["usage_pct"] = round(r["count"] / population * 100, 2) if population else 0.0
        out.setdefault(skill, {}).setdefault(bracket, {})[bucket] = kp_rows
    return out


def _build_skill_centric_archive(
    stat_rows: list[tuple], passive_rows: list[tuple],
    population_rows_by_snapshot: dict[int, tuple[Optional[int], list[tuple]]],
) -> dict[str, Any]:
    """`stat_rows`: (skill_type, skill, bracket, slot, stat_id, mod_kind,
    count, population, slot_holders, v_min, v_max, snapshot_id, days).
    `passive_rows`: (skill_type, skill, bracket, keypassive, count,
    population, snapshot_id, days). `population_rows_by_snapshot`: per
    snapshot_id, `(days_since_league_start, that snapshot's own (skill_type,
    skill, bracket, population, facet_count) rows)` -- the source of truth
    for population, same reasoning as `skill_publish.build_skill_groups`,
    applied per age bucket instead of per snapshot: a (skill_type, skill,
    bracket, bucket) group's population is the SUM of its member snapshots'
    own populations (each counted once, not once per stat/passive row -- same
    population-dedup rule as `_build_skill_mod_archive` above, keyed without
    `archetype` since this table has none)."""
    population_by_key: dict[tuple, int] = defaultdict(int)
    for days, rows in population_rows_by_snapshot.values():
        for skill_type, skill, bracket, population, _facet in rows:
            population_by_key[(skill_type, skill, bracket, _age_bucket(days))] += population

    stat_by_key: dict[tuple, dict[tuple, dict[str, Any]]] = {}
    for skill_type, skill, bracket, slot, stat_id, mod_kind, count, _pop, _slot_holders, v_min, v_max, snapshot_id, days in stat_rows:
        bucket = _age_bucket(days)
        key = (skill_type, skill, bracket, bucket, slot)
        entry = stat_by_key.setdefault(key, {}).setdefault(
            (stat_id, mod_kind), {"stat_id": stat_id, "mod_kind": mod_kind, "count": 0, "value_min": None, "value_max": None},
        )
        entry["count"] += count
        if v_min is not None:
            entry["value_min"] = v_min if entry["value_min"] is None else min(entry["value_min"], v_min)
        if v_max is not None:
            entry["value_max"] = v_max if entry["value_max"] is None else max(entry["value_max"], v_max)

    kp_by_key: dict[tuple, dict[str, dict[str, Any]]] = {}
    for skill_type, skill, bracket, keypassive, count, _pop, snapshot_id, days in passive_rows:
        bucket = _age_bucket(days)
        key = (skill_type, skill, bracket, bucket)
        entry = kp_by_key.setdefault(key, {}).setdefault(keypassive, {"keypassive": keypassive, "count": 0})
        entry["count"] += count

    leaves: dict[tuple, dict[str, Any]] = {}
    for (skill_type, skill, bracket, bucket, slot), stat_map in stat_by_key.items():
        leaf = leaves.setdefault((skill_type, skill, bracket, bucket), {"population": 0, "mods": {}, "passives": []})
        pop = population_by_key.get((skill_type, skill, bracket, bucket), 0)
        rows = sorted(stat_map.values(), key=lambda r: r["count"], reverse=True)
        for r in rows:
            r["population"] = pop
            r["usage_pct"] = round(r["count"] / pop * 100, 2) if pop else 0.0
        leaf["population"] = pop
        leaf["mods"][slot] = rows
    for (skill_type, skill, bracket, bucket), kp_map in kp_by_key.items():
        leaf = leaves.setdefault((skill_type, skill, bracket, bucket), {"population": 0, "mods": {}, "passives": []})
        pop = population_by_key.get((skill_type, skill, bracket, bucket), 0)
        rows = sorted(kp_map.values(), key=lambda r: r["count"], reverse=True)
        for r in rows:
            r["population"] = pop
            r["usage_pct"] = round(r["count"] / pop * 100, 2) if pop else 0.0
        leaf["population"] = pop
        leaf["passives"] = rows

    out: dict[str, Any] = {}
    for (skill_type, skill, bracket, bucket), leaf in leaves.items():
        out.setdefault(skill_type, {}).setdefault(skill, {}).setdefault(bracket, {})[bucket] = leaf
    return out


def build_league_archive(db: LocalDb, game: str, league: str) -> dict[str, Any]:
    skill_rows = db.iter_skill_mod_counts_for_league(game, league)
    mod_rows = db.iter_mod_counts_for_league(game, league)
    passive_rows = db.iter_keypassive_counts_for_league(game, league)

    skill_centric_stat_rows = db.iter_skill_stat_counts_for_league(game, league)
    skill_centric_passive_rows = db.iter_skill_passive_counts_for_league(game, league)
    days_by_snapshot = {r[11]: r[12] for r in skill_centric_stat_rows}
    days_by_snapshot.update({r[6]: r[7] for r in skill_centric_passive_rows})
    population_rows_by_snapshot = {
        snapshot_id: (days, db.iter_skill_populations_for_snapshot(snapshot_id))
        for snapshot_id, days in days_by_snapshot.items()
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "game": game,
        "league": league,
        "league_slug": sanitize_league_filename(league),
        "exported_at": _now_iso(),
        "age_buckets": ["1-21", "22-60", "61+", "unknown"],
        "mod_counts": _build_mod_archive(mod_rows),
        "skill_mod_counts": _build_skill_mod_archive(skill_rows),
        "passives": _build_passive_archive(passive_rows),
        "skills": _build_skill_centric_archive(skill_centric_stat_rows, skill_centric_passive_rows, population_rows_by_snapshot),
    }


def export_league_archive(
    db: LocalDb,
    game: str,
    league: str,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    publisher: Any = None,
) -> Path:
    """Writes the archive to `archive_dir/{game}-{league-slug}.json` on the
    VM (always), and best-effort copies it to Firebase at
    `archive/{game}-{league-slug}.json` if `publisher` is given (spec item
    22: "if it doesn't add burden" -- a publish failure is logged and
    swallowed, never fails the export itself, since the VM copy is already
    the durable one).

    The VM copy is written *compact* (no `indent=`), unlike every other
    published file in this project -- measured live 2026-09-17 against the
    real production archive (poe2 Forbidden Rites, poe1 Allflame, both
    still early in the league): ~49% of the file was pure indentation
    whitespace (thousands of leaf stat rows nested 4-5 dicts deep), with
    zero data difference. `publisher.publish` (used for the Firebase copy)
    is the shared method every other job uses and still writes indented --
    deliberately not touched here, since that's a project-wide convention
    affecting every published file, not an archive-specific decision."""
    data = build_league_archive(db, game, league)
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{game}-{data['league_slug']}.json"
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    size_kb = len(text.encode("utf-8")) / 1024
    log.info("archive: exported %s/%s -> %s (%.1f KB)", game, league, path, size_kb)

    if publisher is not None:
        try:
            publisher.publish(data, f"archive/{game}-{data['league_slug']}.json", cache_control="public, max-age=3600")
        except Exception as e:
            log.warning("archive: could not publish %s to Firebase (kept on VM only): %s", path, e)

    return path
