"""Skill-centric collection run: top-100 characters per poe.ninja skill list.

Phases, each checkpointed in SQLite so a kill / restart / deploy resumes
instead of starting over (a first run takes hours -- 8-10h per game at
1 request / 2s):

  A. Lists    -- fetch every planned skill list (`skill_run_lists` marks a
                 list 'done' atomically with its members).
  B. Details  -- fetch the distinct characters those lists contain (minus the
                 cut ascendancies) into the shared `char_details` cache;
                 every character is persisted the moment it arrives, and one
                 whose stored copy is still fresh (same league, same level,
                 younger than `reuse_max_age_days`) is reused, not refetched.
  C. Counting -- per skill group, read its members' stored details one group
                 at a time (never the whole population in RAM) and write the
                 counts. Idempotent per group.

The plan (skill list + ascendancy cut) is frozen in `skill_runs.plan_json` at
the start, so a resumed run continues the same plan.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from src.fetchers.ninja_builds import DictionaryValidationError, NinjaBuildsFetcher
from src.processing.aggregator import LevelBracket
from src.processing.char_cache import SnapshotProvider, StoreBackedCharacterCache
from src.processing.normalizer import ModNormalizer
from src.processing.skill_counter import count_skill_group
from src.processing.skill_plan import SkillPlan, build_plan
from src.storage.local_db import COLLECTION_METHOD_SKILL, LocalDb

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectConfig:
    min_skill_pct: float = 0.1
    min_ascendancy_pct: float = 0.1
    min_member_list_count: int = 20
    fold_vaal: bool = True
    list_size: int = 100
    reuse_max_age_days: float = 7.0
    resume_max_age_hours: float = 48.0
    max_failed_list_pct: float = 5.0
    max_failed_char_pct: float = 25.0
    snapshot_refresh_seconds: float = 1800.0
    progress_every_lists: int = 25
    progress_every_chars: int = 250

    @staticmethod
    def from_cfg(cfg: dict[str, Any]) -> "CollectConfig":
        c = (cfg.get("skill_meta", {}) or {}).get("collect", {}) or {}
        defaults = CollectConfig()
        return CollectConfig(**{
            f: type(getattr(defaults, f))(c[f]) if f in c else getattr(defaults, f)
            for f in defaults.__dataclass_fields__
        })


def _eta(done: int, total: int, started: float) -> str:
    if done <= 0:
        return "eta n/a"
    remaining = (time.monotonic() - started) / done * (total - done)
    return f"eta {remaining / 3600:.1f}h"


def run_skill_collection(
    *,
    game: str,
    league: str,
    days_since_league_start: Optional[int],
    builds_fetcher: NinjaBuildsFetcher,
    snapshots: SnapshotProvider,
    cache: StoreBackedCharacterCache,
    db: LocalDb,
    normalizer: ModNormalizer,
    level_brackets: list[LevelBracket],
    config: CollectConfig,
) -> dict[str, Any]:
    started = time.monotonic()
    requests = {"facets": 0, "lists": 0}

    # ---- plan: resume the unfinished run, or start a new one --------------
    resumed = db.find_resumable_skill_run(game, league, config.resume_max_age_hours)
    if resumed is not None:
        run_id, snapshot_id = resumed["id"], resumed["snapshot_id"]
        plan = SkillPlan.from_json(resumed["plan"])
        log.info(
            "skill run(%s): RESUMING run %d (started %s): %s",
            game, run_id, resumed["started_at"], db.skill_list_counts(run_id),
        )
    else:
        facets = builds_fetcher.fetch_population_facets(snapshots.get())
        requests["facets"] += 1
        plan = build_plan(
            game, facets, config.min_skill_pct, config.min_ascendancy_pct,
            config.min_member_list_count, config.fold_vaal,
        )
        if not plan.lists:
            raise RuntimeError(f"skill run({game}): plan has no skill lists (population {facets.total}) -- refusing to run")
        snapshot_id = db.insert_snapshot(
            game=game, league=league, days_since_league_start=days_since_league_start,
            sample_total=0, unique_characters_fetched=0, fetch_failed=0,
            collection_method=COLLECTION_METHOD_SKILL,
        )
        run_id = db.create_skill_run(
            game, league, snapshot_id, plan.population_total, plan.to_json(),
            [{"skill_type": l.skill_type, "skill": l.skill, "list_name": l.list_name, "facet_count": l.facet_count}
             for l in plan.lists],
        )
        log.info(
            "skill run(%s): NEW run %d, population %d, %d main + %d spirit skills, %d lists, "
            "ascendancy cut (<%s%% of population): %s",
            game, run_id, plan.population_total, len(plan.skills("main")), len(plan.skills("spirit")),
            len(plan.lists), plan.min_ascendancy_pct, sorted(plan.excluded_classes),
        )

    # ---- phase A: lists ---------------------------------------------------
    pending = db.pending_skill_lists(run_id)
    log.info("skill run(%s): phase A -- %d list(s) to fetch", game, len(pending))
    a_started = time.monotonic()
    failed_lists = 0
    for i, lst in enumerate(pending, start=1):
        try:
            server_total, rows = builds_fetcher.fetch_skill_list(snapshots.get(), lst["skill_type"], lst["list_name"])
            requests["lists"] += 1
        except DictionaryValidationError:
            raise
        except Exception as e:
            failed_lists += 1
            requests["lists"] += 1
            db.mark_skill_list_failed(run_id, lst["skill_type"], lst["list_name"])
            log.warning("skill run(%s): list %s/%s failed: %s", game, lst["skill_type"], lst["list_name"], e)
            continue
        members = [
            {"account": r.account, "name": r.name, "level": r.level, "class": r.char_class, "keypassives": r.keypassives}
            for r in rows[: config.list_size]
        ]
        db.save_skill_list_members(run_id, lst["skill_type"], lst["skill"], lst["list_name"], server_total, members)
        if i % config.progress_every_lists == 0:
            log.info("skill run(%s): lists %d/%d (%s)", game, i, len(pending), _eta(i, len(pending), a_started))

    if pending and failed_lists / len(pending) * 100 > config.max_failed_list_pct:
        # Left 'running' on purpose: the next attempt resumes and retries just
        # the failed lists (a 'failed' run would never be resumed).
        raise RuntimeError(
            f"skill run({game}): {failed_lists}/{len(pending)} lists failed (> {config.max_failed_list_pct}%) -- "
            f"run {run_id} kept resumable state; nothing counted or published"
        )

    # ---- ascendancy cut, logged every run ---------------------------------
    excluded = set(plan.excluded_classes)
    cut = db.member_stats(run_id, excluded)
    log.info(
        "skill run(%s): ascendancy cut removed %d member row(s) / %d unique character(s) "
        "of %d rows / %d unique (classes cut: %s)",
        game, cut["cut_rows"], cut["unique_chars_cut"], cut["member_rows"], cut["unique_chars"], sorted(excluded),
    )

    # ---- phase B: character details ----------------------------------------
    keys = sorted(db.iter_run_member_keys(run_id, excluded), key=lambda k: (k[0], k[1]))
    if not keys:
        db.finish_skill_run(run_id, "failed", {"reason": "no members after ascendancy cut"})
        raise RuntimeError(f"skill run({game}): no characters to fetch (0 members) -- refusing to continue")
    log.info("skill run(%s): phase B -- %d distinct character(s) to load", game, len(keys))
    b_started = time.monotonic()
    for i, (account, name, level) in enumerate(keys, start=1):
        cache.get(account, name, level)
        if i % config.progress_every_chars == 0:
            log.info(
                "skill run(%s): characters %d/%d -- fetched %d, reused %d, failed %d (%s)",
                game, i, len(keys), cache.fetched_count, cache.reused_count, cache.failed_count,
                _eta(i, len(keys), b_started),
            )
    loaded = cache.fetched_count + cache.reused_count
    if loaded == 0:
        db.finish_skill_run(run_id, "failed", {"reason": "0 character details loaded"})
        raise RuntimeError(f"skill run({game}): 0 of {len(keys)} character details loaded -- refusing to count")
    if cache.failed_count / len(keys) * 100 > config.max_failed_char_pct:
        # Left 'running' (resumable) for the same reason as the list guard.
        raise RuntimeError(
            f"skill run({game}): {cache.failed_count}/{len(keys)} character fetches failed "
            f"(> {config.max_failed_char_pct}%) -- run {run_id} left resumable, nothing counted"
        )
    log.info(
        "skill run(%s): characters -- fetched %d NEW, reused %d from the shared cache, failed %d",
        game, cache.fetched_count, cache.reused_count, cache.failed_count,
    )

    # ---- phase C: counting -------------------------------------------------
    total_stat_rows = total_passive_rows = counted_total = missing_total = 0
    groups = db.run_skill_groups(run_id)
    for skill_type, skill in groups:
        members = db.iter_skill_members(run_id, skill_type, skill)
        result = count_skill_group(
            skill_type, skill, members, lambda a, n: cache.get_stored(a, n), normalizer, level_brackets,
            excluded, plan.group_counts.get((skill_type, skill), 0),
        )
        db.replace_skill_counts_for_snapshot(
            snapshot_id, game, days_since_league_start, skill_type, skill,
            result.stat_rows, result.passive_rows, result.population_rows,
        )
        total_stat_rows += len(result.stat_rows)
        total_passive_rows += len(result.passive_rows)
        counted_total += result.counted
        missing_total += result.missing_detail
    if counted_total == 0:
        db.finish_skill_run(run_id, "failed", {"reason": "counted 0 characters"})
        raise RuntimeError(f"skill run({game}): counted 0 characters across {len(groups)} skill groups")

    # ---- finalize ------------------------------------------------------------
    db.update_snapshot_summary(
        snapshot_id, sample_total=cut["member_rows"] - cut["cut_rows"],
        unique_characters_fetched=len(keys) - cache.failed_count, fetch_failed=cache.failed_count,
    )
    stats = {
        "run_id": run_id, "snapshot_id": snapshot_id, "resumed": resumed is not None,
        "duration_seconds": round(time.monotonic() - started, 1),
        "population_total": plan.population_total,
        "main_skills": len(plan.skills("main")), "spirit_skills": len(plan.skills("spirit")),
        "lists_planned": len(plan.lists), "lists_failed_this_attempt": failed_lists,
        "requests_this_attempt": {
            **requests, "characters": cache.fetched_count, "index_state": snapshots.resolve_count,
            "total": requests["facets"] + requests["lists"] + cache.fetched_count + snapshots.resolve_count,
        },
        "characters_distinct": len(keys), "characters_fetched_new": cache.fetched_count,
        "characters_reused": cache.reused_count, "characters_failed": cache.failed_count,
        "ascendancy_cut": {**cut, "classes": sorted(excluded)},
        "characters_counted_memberships": counted_total, "memberships_missing_detail": missing_total,
        "skill_groups": len(groups), "stat_rows": total_stat_rows, "passive_rows": total_passive_rows,
    }
    db.finish_skill_run(run_id, "finished", stats)
    log.info("skill run(%s): FINISHED %s", game, stats)
    return stats
