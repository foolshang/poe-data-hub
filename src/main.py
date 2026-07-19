"""Entry point.

    python -m src.main --run-prices-once
    python -m src.main --run-meta-once
    python -m src.main --run-cleanup-once
    python -m src.main --run-news-once
    python -m src.main --serve      # persistent scheduler (systemd uses this)
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.fetchers import deals, news, ninja_prices, scout_prices
from src.fetchers.league_dates import LeagueDatesFetcher
from src.fetchers.league_detector import LeagueDetector
from src.fetchers.ninja_builds import NinjaBuildsFetcher
from src.fetchers.ninja_chars import DedupedCharacterCache, NinjaCharsFetcher
from src.fetchers.ninja_prices import NinjaPricesFetcher
from src.fetchers.scout_prices import ScoutPricesFetcher
from src.processing.aggregator import aggregate_jewels, aggregate_slot, compute_archetype_distribution
from src.processing.archetyper import Archetyper
from src.processing.normalizer import ModNormalizer
from src.storage.local_db import LocalDb
from src.storage.publisher import (
    PricesPublisher,
    build_index_schema,
    build_meta_schema,
    build_mtx_schema,
    build_news_schema,
    build_prices_schema,
    sanitize_league_filename,
)
from src.utils import job_lock
from src.utils.config import load_config
from src.utils.http_client import HttpClient
from src.utils.rate_limiter import RateLimiter

log = logging.getLogger("poe_data_hub")

STATUS_BLOB_PATH = "status.json"
MTX_BLOB_PATH = "mtx/latest.json"
# Must match tools/refresh_stats.py's TARGETS blob_paths exactly -- that's
# what actually publishes these files (phase 10: PoE1 added alongside PoE2).
META_STATS_CACHE_BLOB_PATH_BY_GAME = {
    "poe2": "internal/stats_cache.json",
    "poe1": "internal/stats_cache_poe1.json",
}


def _meta_blob_path(game: str) -> str:
    return f"{game}/meta/latest.json"


def _meta_dated_blob_path(game: str, date_str: str) -> str:
    return f"{game}/meta/{date_str}.json"


def _prices_blob_path(game: str) -> str:
    return f"{game}/prices/latest.json"


def _event_blob_path(game: str, slug: str) -> str:
    return f"{game}/prices/events/{slug}.json"


def _index_blob_path(game: str) -> str:
    return f"{game}/prices/index.json"


def _hardcore_blob_path(game: str) -> str:
    return f"{game}/prices/hardcore/latest.json"


def _hardcore_prefix(game: str) -> str:
    return f"{game}/prices/hardcore/"


def _news_blob_path(game: str) -> str:
    return f"{game}/news/latest.json"


_shared_rate_limiter: Optional[RateLimiter] = None
_shared_rate_limiter_lock = threading.Lock()


def _get_shared_rate_limiter(cfg: dict[str, Any]) -> RateLimiter:
    """One RateLimiter per process, reused by every job (prices/meta/news/
    cleanup). Needed so a 429+Retry-After learned by one job's HttpClient
    (via RateLimiter.note_retry_after) is immediately visible to every other
    job's concurrently-running thread hitting the same host -- previously
    each `_build_http_client` call made a fresh, isolated RateLimiter, so
    concurrent jobs backed off independently instead of together (see
    CLAUDE.md's 2026-07-15 incident)."""
    global _shared_rate_limiter
    with _shared_rate_limiter_lock:
        if _shared_rate_limiter is None:
            _shared_rate_limiter = RateLimiter(default_interval_seconds=2.0, per_host=cfg.get("rate_limits", {}))
    return _shared_rate_limiter


def _build_http_client(cfg: dict[str, Any]) -> HttpClient:
    http_cfg = cfg.get("http", {})
    return HttpClient(
        _get_shared_rate_limiter(cfg),
        max_retries=http_cfg.get("max_retries", 3),
        timeout_seconds=http_cfg.get("timeout_seconds", 15),
    )


def _ping_healthcheck(http: HttpClient, url: Optional[str], job_name: str) -> None:
    """Layer-1 dead-man-switch ping (healthchecks.io or compatible) -- fired
    only after this job's Firebase publish + readback-verify has already
    succeeded, not just "the job finished" (see CLAUDE.md). `url` is
    optional; blank/missing means this layer is off for that job, not an
    error. A failed ping must never fail the job itself."""
    if not url:
        return
    try:
        http.get_bytes(url)
    except Exception as e:
        log.warning("%s job: healthcheck ping failed (%s) -- not fatal", job_name, e)


def _fetch_and_publish_league(
    http: HttpClient, publisher: PricesPublisher, game: str, league: str, blob_path: str
) -> tuple[dict[str, Any], bool]:
    """Fetch ninja(+scout for poe2) prices for a single league and publish
    them to `blob_path` (either the main `latest.json` or an
    `events/{slug}.json`) using the exact same schema-8.1 shape either way."""
    ninja_entries: list[dict[str, Any]] = []
    ninja_per_category: Optional[dict[str, dict[str, Any]]] = None
    ninja_ok = True
    try:
        ninja_entries, ninja_per_category = NinjaPricesFetcher(http, game=game).fetch_all(league)
        failed_categories = [c for c, v in ninja_per_category.items() if not v["ok"]]
        log.info(
            "prices job(%s/%s): ninja fetched %d entries across %d categories (%d failed: %s)",
            game, league, len(ninja_entries), len(ninja_per_category), len(failed_categories), failed_categories,
        )
    except Exception as e:
        ninja_ok = False
        log.error("prices job(%s/%s): ninja fetch failed entirely: %s", game, league, e, exc_info=True)

    # poe2scout.com is PoE2-only (spec 3.2) -- PoE1 never queries it, so its
    # sources-status key is omitted for poe1 rather than faked (see
    # build_prices_schema's uses_scout handling).
    scout_entries: Optional[list[dict[str, Any]]] = None
    scout_per_category: Optional[dict[str, dict[str, Any]]] = None
    scout_ok: Optional[bool] = None
    scout_bucket_of = None
    if game == "poe2":
        scout_entries = []
        scout_ok = True
        try:
            scout_entries, scout_per_category = ScoutPricesFetcher(http).fetch_all(league)
            log.info(
                "prices job(%s/%s): scout fetched %d entries across %d categories",
                game, league, len(scout_entries), len(scout_per_category),
            )
        except Exception as e:
            scout_ok = False
            log.error("prices job(%s/%s): scout fetch failed entirely: %s", game, league, e, exc_info=True)
        scout_bucket_of = scout_prices.category_bucket

    previous = publisher.fetch_previous(blob_path)
    data = build_prices_schema(
        league=league,
        ninja_entries=ninja_entries,
        ninja_ok=ninja_ok,
        ninja_bucket_of=lambda c: ninja_prices.category_bucket(c, game=game),
        scout_entries=scout_entries,
        scout_ok=scout_ok,
        scout_bucket_of=scout_bucket_of,
        previous=previous,
        ninja_per_category=ninja_per_category,
        scout_per_category=scout_per_category,
    )
    publisher.publish(data, blob_path, cache_control="public, max-age=300")
    log.info(
        "prices job(%s/%s): published %d currency + %d item entries -> %s",
        game, league, len(data["currency"]), len(data["items"]), blob_path,
    )

    ok = ninja_ok and (scout_ok if scout_ok is not None else True)
    return data, ok


def _run_prices_for_game(http: HttpClient, publisher: PricesPublisher, cfg: dict[str, Any], game: str) -> dict[str, Any]:
    league_cfg = cfg.get("league", {})
    detector = LeagueDetector(
        http,
        game=game,
        league_override=league_cfg.get(f"{game}_override", ""),
        cache_ttl_seconds=league_cfg.get("cache_ttl_seconds", 12 * 60 * 60),
    )
    league_result = detector.detect()
    log.info("prices job(%s): detected league=%s events=%s", game, league_result.main, league_result.events)

    main_data, main_ok = _fetch_and_publish_league(http, publisher, game, league_result.main, _prices_blob_path(game))

    events_index: list[dict[str, str]] = []
    events_ok = True
    for event_league in league_result.events:
        try:
            slug = sanitize_league_filename(event_league)
            event_data, event_ok = _fetch_and_publish_league(
                http, publisher, game, event_league, _event_blob_path(game, slug)
            )
            events_index.append(
                {"league": event_league, "path": _event_blob_path(game, slug), "generated_at": event_data["generated_at"]}
            )
            events_ok = events_ok and event_ok
        except Exception as e:
            events_ok = False
            log.error("prices job(%s): event league %s failed entirely: %s", game, event_league, e, exc_info=True)

    hardcore_index: Optional[dict[str, str]] = None
    hardcore_ok = True
    if league_result.hardcore:
        try:
            hardcore_data, hardcore_ok = _fetch_and_publish_league(
                http, publisher, game, league_result.hardcore, _hardcore_blob_path(game)
            )
            hardcore_index = {
                "league": league_result.hardcore,
                "path": _hardcore_blob_path(game),
                "generated_at": hardcore_data["generated_at"],
            }
        except Exception as e:
            hardcore_ok = False
            log.error("prices job(%s): hardcore league %s failed entirely: %s", game, league_result.hardcore, e, exc_info=True)
    else:
        # No lingering stale file: index.hardcore == null must mean the file
        # genuinely doesn't exist, not "we didn't bother updating it today".
        deleted = publisher.delete_blobs_not_in(_hardcore_prefix(game), keep_names=frozenset())
        log.info(
            "prices job(%s): no hardcore variant of %s found this cycle -- index.hardcore will be null%s",
            game, league_result.main, f" (removed {deleted} stale file(s))" if deleted else "",
        )

    index_data = build_index_schema(
        main_league=league_result.main,
        main_path=_prices_blob_path(game),
        main_generated_at=main_data["generated_at"],
        events=events_index,
        hardcore=hardcore_index,
    )
    publisher.publish(index_data, _index_blob_path(game), cache_control="public, max-age=300")
    log.info(
        "prices job(%s): published index with %d event league(s), hardcore=%s",
        game, len(events_index), league_result.hardcore or "none",
    )

    return {
        "ok": main_ok and events_ok and hardcore_ok,
        "league": league_result.main,
        "events": league_result.events,
        "hardcore": league_result.hardcore,
        "sources": main_data["sources"],
        "generated_at": main_data["generated_at"],
    }


def run_prices_once(cfg: dict[str, Any]) -> dict[str, Any]:
    http = _build_http_client(cfg)
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

    games = cfg.get("game_versions", ["poe2"])
    per_game: dict[str, dict[str, Any]] = {}
    overall_ok = True
    for game in games:
        try:
            per_game[game] = _run_prices_for_game(http, publisher, cfg, game)
            overall_ok = overall_ok and per_game[game]["ok"]
        except Exception as e:
            overall_ok = False
            log.error("prices job(%s): failed entirely: %s", game, e, exc_info=True)

    # One Layer-1 ping per overall job run, not per game/league -- it means
    # "the prices job ran and published something", same semantics as before
    # PoE1/events existed (see CLAUDE.md two-layer monitoring design).
    _ping_healthcheck(http, cfg.get("healthchecks", {}).get("prices_ping_url"), "prices")

    _update_status(publisher, ok=overall_ok, per_game=per_game)
    return per_game


def run_news_once(cfg: dict[str, Any]) -> dict[str, Any]:
    """GGG-forum-sourced news (schema v2, 2026-07-18 redesign) per game, and
    PoE1's Deal of the Day, in one job (approved cadence: every 5 minutes,
    much more frequent than prices/meta since news is time-sensitive).
    Which entries a consumer has already *posted* is still their state
    (CLAUDE.md) -- but unlike v1, this job passes the previous publish's
    entries into `news.fetch_forum_news` so it can skip re-scraping content
    for threads it already knows about (see that function's docstring)."""
    http = _build_http_client(cfg)
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

    overall_ok = True
    per_game: dict[str, dict[str, Any]] = {}
    for game in cfg.get("game_versions", ["poe2"]):
        if game not in news.GGG_LISTING_URLS:
            continue
        blob_path = _news_blob_path(game)
        previous = publisher.fetch_previous(blob_path)
        previous_entries = previous.get("entries", []) if previous else []
        entries: list[dict[str, Any]] = []
        ok = True
        try:
            entries = news.fetch_forum_news(http, game, previous_entries)
            log.info("news job(%s): fetched %d entries", game, len(entries))
        except Exception as e:
            ok = False
            log.error("news job(%s): fetch failed entirely: %s", game, e, exc_info=True)

        data = build_news_schema(entries, ok=ok, previous=previous)
        publisher.publish(data, blob_path, cache_control="public, max-age=120")
        log.info("news job(%s): published %d entries -> %s", game, len(data["entries"]), blob_path)
        per_game[game] = {"ok": ok, "generated_at": data["generated_at"], "count": len(data["entries"])}
        overall_ok = overall_ok and ok

    mtx_previous = publisher.fetch_previous(MTX_BLOB_PATH)
    fetched_deals: list[dict[str, Any]] = []
    deals_ok = True
    try:
        fetched_deals = deals.fetch_deals(http)
        log.info("news job(mtx): fetched %d deals", len(fetched_deals))
    except Exception as e:
        deals_ok = False
        log.error("news job(mtx): fetch failed entirely: %s", e, exc_info=True)

    mtx_data = build_mtx_schema(fetched_deals, ok=deals_ok, previous=mtx_previous)
    publisher.publish(mtx_data, MTX_BLOB_PATH, cache_control="public, max-age=120")
    log.info("news job(mtx): published %d deals -> %s", len(mtx_data["deals"]), MTX_BLOB_PATH)
    overall_ok = overall_ok and deals_ok

    # One ping per overall job run, same Layer-1 semantics as prices/meta.
    _ping_healthcheck(http, cfg.get("healthchecks", {}).get("news_ping_url"), "news")

    return {"games": per_game, "mtx": {"ok": deals_ok, "generated_at": mtx_data["generated_at"], "count": len(mtx_data["deals"])}, "ok": overall_ok}


def _run_meta_for_game(http: HttpClient, publisher: PricesPublisher, cfg: dict[str, Any], game: str) -> dict[str, Any]:
    job_start = time.monotonic()

    league_cfg = cfg.get("league", {})
    detector = LeagueDetector(
        http,
        game=game,
        league_override=league_cfg.get(f"{game}_override", ""),
        cache_ttl_seconds=league_cfg.get("cache_ttl_seconds", 12 * 60 * 60),
    )
    league_result = detector.detect()
    league = league_result.main
    log.info("meta job(%s): detected league=%s events=%s", game, league, league_result.events)

    league_dates = LeagueDatesFetcher(http, game=game).fetch(league)
    days_since_league_start: Optional[int] = (
        (datetime.now(timezone.utc) - league_dates.start_at).days if league_dates.start_at else None
    )
    log.info("meta job(%s): league_dates start_at=%s end_at=%s", game, league_dates.start_at, league_dates.end_at)

    builds_fetcher = NinjaBuildsFetcher(http, game=game)
    snapshot = builds_fetcher.resolve_snapshot(league)

    by_slot, per_slot_counts = builds_fetcher.fetch_all(snapshot)
    log.info("meta job(%s): fetched %d slot lists: %s", game, len(by_slot), per_slot_counts)

    firebase_bucket = cfg["firebase"]["bucket"]
    normalizer = ModNormalizer(
        http,
        game=game,
        cache_ttl_seconds=cfg.get("ggg", {}).get("stats_cache_ttl_seconds", 7 * 24 * 60 * 60),
        firebase_stats_url=f"https://storage.googleapis.com/{firebase_bucket}/{META_STATS_CACHE_BLOB_PATH_BY_GAME[game]}",
    )
    normalizer.load()
    log.info("meta job(%s): stats loaded from %s (fetched_at=%s)", game, normalizer.stats_source, normalizer.stats_fetched_at)
    archetyper = Archetyper()

    chars_fetcher = NinjaCharsFetcher(http, game=game)
    cache = DedupedCharacterCache(chars_fetcher, snapshot)

    def get_detail(account: str, name: str):
        return cache.get(account, name)

    all_mod_counts = []
    per_slot_sample: dict[str, int] = {}
    for slot, char_refs in by_slot.items():
        if slot == "jewel":
            jewel_counts, denom_by_base = aggregate_jewels(char_refs, get_detail, archetyper, normalizer)
            all_mod_counts.extend(jewel_counts)
            per_slot_sample.update(denom_by_base)
        else:
            all_mod_counts.extend(aggregate_slot(slot, char_refs, get_detail, archetyper, normalizer))
            per_slot_sample[slot] = len(char_refs)

    unique_refs_by_key = {(r.account, r.name): r for refs in by_slot.values() for r in refs}
    archetype_distribution = compute_archetype_distribution(list(unique_refs_by_key.values()), get_detail, archetyper)

    db = LocalDb()
    try:
        snapshot_id = db.insert_snapshot(
            game=game,
            league=league,
            days_since_league_start=days_since_league_start,
            sample_total=sum(per_slot_counts.values()),
            unique_characters_fetched=cache.fetched_count,
            fetch_failed=cache.failed_count,
        )
        for (account, name), ref in unique_refs_by_key.items():
            detail = get_detail(account, name)
            if detail is None:
                continue
            archetype = archetyper.classify(
                detail.main_skill, next((i.base_type for i in detail.items if i.slot == "weapon"), None)
            )
            db.insert_char_item(
                snapshot_id, account, name, detail.level, detail.char_class, archetype,
                [dataclasses.asdict(i) for i in detail.items],
            )
        db.insert_mod_counts(snapshot_id, [dataclasses.asdict(m) for m in all_mod_counts])
        db.commit()
    finally:
        db.close()

    data = build_meta_schema(
        league=league,
        days_since_league_start=days_since_league_start,
        per_slot_sample=per_slot_sample,
        unique_characters_fetched=cache.fetched_count,
        fetch_failed=cache.failed_count,
        archetype_distribution=archetype_distribution,
        stat_dictionary=normalizer.stat_dictionary(),
        mods=[dataclasses.asdict(m) for m in all_mod_counts],
        min_count=cfg.get("meta", {}).get("min_mod_count", 5),
    )

    dated_path = _meta_dated_blob_path(game, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    publisher.publish(data, _meta_blob_path(game), cache_control="public, max-age=3600")
    publisher.publish(data, dated_path, cache_control="public, max-age=3600")
    log.info("meta job(%s): published %d mods (%d before count>=5 filter)", game, len(data["mods"]), len(all_mod_counts))

    unmapped_pct = (
        round(normalizer.unmapped_count / normalizer.processed_count * 100, 2)
        if normalizer.processed_count else 0.0
    )
    peak_rss_mb = _peak_memory_mb()
    log.info("meta job(%s): peak RSS = %s MB", game, peak_rss_mb if peak_rss_mb is not None else "unknown (non-Unix)")

    return {
        "data": data,
        "end_at": league_dates.end_at,
        "summary": {
            "duration_seconds": round(time.monotonic() - job_start, 1),
            "sample_total": sum(per_slot_counts.values()),
            "unique_characters_fetched": cache.fetched_count,
            "dedupe_savings": sum(per_slot_counts.values()) - cache.fetched_count,
            "characters_failed": cache.failed_count,
            "mods_processed": normalizer.processed_count,
            "unmapped_mod_pct": unmapped_pct,
            "peak_rss_mb": peak_rss_mb,
            "stats_source": normalizer.stats_source,
            "stats_cache_fetched_at": normalizer.stats_fetched_at,
        },
    }


def run_meta_once(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Runs poe2 then poe1 sequentially in one job invocation (approved:
    same nightly cron slot, one game after the other -- see SPEC.md for the
    measured combined wall-clock time and peak RAM). One game's failure
    never blocks or corrupts the other's publish; one healthchecks.io ping
    fires only after both have been attempted, so a failure in either game
    is visible (task 7).

    Guarded by job_lock so the scheduler's own cron and a manually launched
    `--run-meta-once` (or two overlapping manual runs) can never race the
    same rate-limited hosts concurrently -- both call this exact function,
    so locking here covers both callers. A second attempt while one is
    already running skips cleanly rather than colliding (see CLAUDE.md's
    2026-07-15 incident, where exactly this collision stalled poe.ninja
    requests for hours via duplicate, uncoordinated retries)."""
    try:
        with job_lock.acquire("meta"):
            return _run_meta_once_locked(cfg)
    except job_lock.JobAlreadyRunning as e:
        log.warning(
            "meta job: skip -- previous run still active (pid=%s, started_at=%s)",
            e.holder.pid, e.holder.started_at,
        )
        return {}


def _run_meta_once_locked(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    http = _build_http_client(cfg)
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

    _mark_job_running(publisher, "meta")
    try:
        results: dict[str, dict[str, Any]] = {}
        for game in cfg.get("game_versions", ["poe2"]):
            try:
                results[game] = _run_meta_for_game(http, publisher, cfg, game)
            except Exception as e:
                log.error("meta job(%s): failed entirely: %s", game, e, exc_info=True)

        _ping_healthcheck(http, cfg.get("healthchecks", {}).get("meta_ping_url"), "meta")
        _update_meta_status(publisher, results)

        return {game: r["data"] for game, r in results.items()}
    finally:
        _mark_job_finished(publisher, "meta")


def _mark_job_running(publisher: PricesPublisher, job_name: str) -> None:
    """Records that `job_name` has started, in `status["running_jobs"]`, so
    a human can check status.json before launching a manual CLI run instead
    of SSHing in to guess at `ps`/journalctl (see CLAUDE.md's 2026-07-15
    incident, where exactly that guesswork led to a collision). Paired with
    job_lock (which is the actual enforcement) -- this is visibility only."""
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    status.setdefault("running_jobs", {})[job_name] = {
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def _mark_job_finished(publisher: PricesPublisher, job_name: str) -> None:
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    status.setdefault("running_jobs", {}).pop(job_name, None)
    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def _peak_memory_mb() -> Optional[float]:
    """Peak resident set size of this process since start, in MB. Linux's
    `ru_maxrss` is in KB (macOS uses bytes, but this only runs on the Linux
    deploy target); returns None on platforms without the `resource` module
    (e.g. Windows dev machines)."""
    try:
        import resource
    except ImportError:
        return None
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def _update_status(publisher: PricesPublisher, ok: bool, per_game: dict[str, dict[str, Any]]) -> None:
    """Nested per-game shape (`status["prices"][game]`) since phase 7 -- a
    game that failed entirely this run (not present in `per_game`) simply
    keeps its last-known status entry untouched, which is the same
    stale-while-error behavior the per-entry `"stale": true` price fields
    already use. tools/heartbeat_check.py only reads `status["last_meta"]`
    and prices/latest.json's own generated_at directly, never this key, so
    this restructuring doesn't affect it."""
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status.setdefault("prices", {})
    for game, result in per_game.items():
        status["prices"][game] = {
            "generated_at": result["generated_at"],
            "league": result["league"],
            "events": result["events"],
            "hardcore": result.get("hardcore"),
            "sources": result["sources"],
        }
    if not ok:
        status.setdefault("errors", []).append(
            {"ts": now, "job": "prices", "detail": {g: r["sources"] for g, r in per_game.items()}}
        )
        status["errors"] = status["errors"][-10:]
    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


STATS_CACHE_MAX_AGE_DAYS = 30
UNMAPPED_HISTORY_LEN = 7


def _check_stats_cache_stale(
    unmapped_history: list[float], stats_fetched_at: Optional[float], unmapped_pct: float
) -> list[str]:
    """Returns a list of human-readable reasons the stats cache looks stale
    (empty list = looks fine). Two independent checks (spec): cache age, and
    this run's unmapped% blowing past the recent baseline."""
    reasons = []
    if stats_fetched_at is not None:
        age_days = (time.time() - stats_fetched_at) / 86400
        if age_days > STATS_CACHE_MAX_AGE_DAYS:
            reasons.append(f"stats cache is {age_days:.1f} days old (> {STATS_CACHE_MAX_AGE_DAYS})")
    if unmapped_history:
        avg = sum(unmapped_history) / len(unmapped_history)
        if avg > 0 and unmapped_pct > 2 * avg:
            reasons.append(f"unmapped_mod_pct {unmapped_pct} > 2x recent average {avg:.2f}")
    return reasons


def _update_meta_status(publisher: PricesPublisher, results: dict[str, dict[str, Any]]) -> None:
    """Nested per-game shape (`status["meta"][game]`, `status["meta_unmapped_history"][game]`)
    since phase 10 -- a game absent from `results` (failed entirely this run)
    simply keeps its last-known status entry untouched, same stale-while-error
    precedent as `_update_status`'s prices restructuring (phase 7).

    `meta_unmapped_history` was a flat list (PoE2-only) before this phase;
    the isinstance check below resets it to `{}` the first time this runs
    against an old-shape status.json, losing that rolling baseline once --
    acceptable (it's a monitoring baseline, not data) and self-healing within
    `UNMAPPED_HISTORY_LEN` runs, same as the old flat prices-status keys were
    left as harmless cruft rather than migrated in phase 7."""
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    status.setdefault("meta", {})
    if not isinstance(status.get("meta_unmapped_history"), dict):
        status["meta_unmapped_history"] = {}

    new_errors = []
    for game, result in results.items():
        data = result["data"]
        end_at = result["end_at"]
        summary = result["summary"]

        history = status["meta_unmapped_history"].get(game, [])
        stale_reasons = _check_stats_cache_stale(history, summary.get("stats_cache_fetched_at"), summary["unmapped_mod_pct"])
        history.append(summary["unmapped_mod_pct"])
        status["meta_unmapped_history"][game] = history[-UNMAPPED_HISTORY_LEN:]

        status["meta"][game] = {
            "last_meta": data["generated_at"],
            "league": data["league"],
            "league_end_at": end_at.strftime("%Y-%m-%dT%H:%M:%SZ") if end_at else None,
            "meta_job_summary": summary,
            "stats_cache_stale": bool(stale_reasons),
        }

        if stale_reasons:
            new_errors.append(
                {"ts": data["generated_at"], "job": f"meta({game})", "detail": "stats_cache_stale: " + "; ".join(stale_reasons)}
            )
        if summary["unmapped_mod_pct"] >= 5.0:
            new_errors.append(
                {"ts": data["generated_at"], "job": f"meta({game})", "detail": f"unmapped_mod_pct={summary['unmapped_mod_pct']} >= 5% threshold"}
            )

    if new_errors:
        status.setdefault("errors", []).extend(new_errors)
        status["errors"] = status["errors"][-10:]

    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def run_cleanup_once(cfg: dict[str, Any]) -> None:
    """Daily cleanup (spec 9): char_items > 90d, meta/*.json on Firebase
    > 30d (never "latest.json"), data/failed_responses/* > 30d, and
    (phase 7) event-league price files for leagues that are no longer
    active. Each step is independent and best-effort — one failing doesn't
    block the others."""
    retention_cfg = cfg.get("retention", {})

    db = LocalDb()
    try:
        deleted_rows = db.cleanup_old_char_items(older_than_days=retention_cfg.get("char_items_days", 90))
        log.info("cleanup job: deleted %d old char_items rows", deleted_rows)
    finally:
        db.close()

    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))
    for game in cfg.get("game_versions", ["poe2"]):
        deleted_meta = publisher.delete_old_blobs(
            f"{game}/meta/", older_than_days=retention_cfg.get("meta_files_days", 30), keep_names=frozenset({"latest.json"})
        )
        log.info("cleanup job(%s): deleted %d old meta files from Firebase", game, deleted_meta)

    deleted_failed = _cleanup_old_local_files(
        Path("data/failed_responses"), older_than_days=retention_cfg.get("failed_responses_days", 30)
    )
    log.info("cleanup job: deleted %d old failed_response files", deleted_failed)

    http = _build_http_client(cfg)
    league_cfg = cfg.get("league", {})
    for game in cfg.get("game_versions", ["poe2"]):
        try:
            detector = LeagueDetector(
                http,
                game=game,
                league_override=league_cfg.get(f"{game}_override", ""),
                cache_ttl_seconds=league_cfg.get("cache_ttl_seconds", 12 * 60 * 60),
            )
            active_events = detector.detect().events
        except Exception as e:
            log.warning(
                "cleanup job(%s): could not determine active event leagues (%s) -- "
                "skipping event-file cleanup this run rather than risk deleting a live one",
                game, e,
            )
            continue
        keep = frozenset(f"{sanitize_league_filename(ev)}.json" for ev in active_events)
        deleted_events = publisher.delete_blobs_not_in(f"{game}/prices/events/", keep_names=keep)
        log.info("cleanup job(%s): deleted %d stale event price file(s), kept %d active", game, deleted_events, len(keep))


def _cleanup_old_local_files(directory: Path, older_than_days: int) -> int:
    if not directory.exists():
        return 0
    cutoff = time.time() - older_than_days * 86400
    deleted = 0
    for path in directory.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError as e:
            log.warning("cleanup job: could not delete %s: %s", path, e)
    return deleted


def _setup_logging(cfg: dict[str, Any]) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()  # journald picks this up under systemd
    console.setFormatter(fmt)
    root.addHandler(console)

    Path("data").mkdir(parents=True, exist_ok=True)
    rotate_days = cfg.get("retention", {}).get("log_rotate_days", 7)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        "data/hub.log", when="D", interval=1, backupCount=rotate_days, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def _safe_run(fn, cfg: dict[str, Any], job_name: str) -> None:
    try:
        fn(cfg)
    except Exception:
        log.error("%s job crashed", job_name, exc_info=True)


def _compute_meta_trigger(schedule_cfg: dict[str, Any], today: Optional[date] = None) -> tuple[BaseTrigger, bool]:
    """Returns (trigger, is_league_start_active) for the meta job's cadence
    right now. `today` defaults to the current Asia/Bangkok calendar date
    when not passed -- an explicit parameter purely so tests can pin it
    without mocking `datetime.now` globally (same pattern
    `_check_stats_cache_stale` already uses instead of a time-mocking
    dependency).

    `schedule.league_start_mode` (start_date/end_date/meta_runs_per_day,
    spec 9) overrides the manual `meta_runs_per_day_league_start` knob
    whenever today falls within [start_date, end_date] (both inclusive,
    Asia/Bangkok calendar dates -- explicit on purpose, see the
    `momentTimezone` lesson in `src/fetchers/news.py`: never assume a bare
    date means what the reader's own locale assumes). Outside that window,
    or if the block is missing/malformed, falls back to the existing
    manual-knob logic unchanged -- that knob stays available as an escape
    hatch for an unplanned/delayed league. A malformed block is a genuine
    misconfiguration (logged at ERROR, loud on purpose) but never crashes
    the scheduler over a config typo."""
    if today is None:
        today = datetime.now(ZoneInfo("Asia/Bangkok")).date()

    league_start_cfg = schedule_cfg.get("league_start_mode")
    if league_start_cfg:
        try:
            start = date.fromisoformat(league_start_cfg["start_date"])
            end = date.fromisoformat(league_start_cfg["end_date"])
            runs_per_day = league_start_cfg["meta_runs_per_day"]
            if start > end:
                raise ValueError(f"start_date {start} is after end_date {end}")
        except (KeyError, ValueError, TypeError) as e:
            log.error(
                "scheduler: malformed schedule.league_start_mode (%s) -- falling back to meta_runs_per_day_league_start",
                e,
            )
        else:
            if start <= today <= end:
                return IntervalTrigger(hours=24 / runs_per_day), True

    meta_runs_per_day = schedule_cfg.get("meta_runs_per_day_league_start", 1)
    meta_hour = schedule_cfg.get("meta_cron_hour_thailand", 2)
    if meta_runs_per_day <= 1:
        return CronTrigger(hour=meta_hour, minute=0, timezone="Asia/Bangkok"), False
    return IntervalTrigger(hours=24 / meta_runs_per_day), False


# Tracks whether league_start_mode is currently active, so
# _check_league_start_mode only reschedules the meta job (and rewrites
# status.json) on an actual state change, not every hourly check. Same
# module-level-singleton style as _shared_rate_limiter.
_league_start_mode_active: Optional[bool] = None


def _update_league_start_mode_status(publisher: PricesPublisher, active: bool) -> None:
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    status["league_start_mode"] = "active" if active else "inactive"
    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def _check_league_start_mode(
    scheduler: BlockingScheduler, cfg: dict[str, Any], publisher: PricesPublisher, today: Optional[date] = None,
) -> None:
    """Runs hourly, independent of the meta job's own cadence on purpose:
    checking only "when the meta job is about to run" would leave up to a
    full day of detection lag while still on the once-daily cron leading up
    to a league (it wouldn't re-fire until the next day's 2am slot),
    defeating the point of an automatic same-day switch. Rescheduling a
    running APScheduler job only changes its *next* fire-time computation --
    it never affects an in-flight invocation, and `_run_meta_once_locked`'s
    job_lock already prevents any overlap regardless (CLAUDE.md's
    2026-07-15 incident). `today` is a passthrough to `_compute_meta_trigger`,
    same test-only purpose."""
    global _league_start_mode_active
    trigger, active = _compute_meta_trigger(cfg.get("schedule", {}), today=today)

    if active != _league_start_mode_active:
        scheduler.reschedule_job("meta", trigger=trigger)
        log.info(
            "scheduler: league_start_mode %s -> %s, meta job rescheduled to %s",
            "active" if _league_start_mode_active else "inactive",
            "active" if active else "inactive",
            trigger,
        )
        _league_start_mode_active = active
        _update_league_start_mode_status(publisher, active)


def run_scheduler(cfg: dict[str, Any]) -> None:
    """Persistent scheduler process (spec 9): prices every N minutes, meta on
    a Thailand-time cron (or several evenly-spaced runs/day during
    `league_start_mode`'s configured date range, or via the manual
    `meta_runs_per_day_league_start` escape hatch), cleanup daily. Jobs run
    independently -- a slow meta run never blocks the prices job."""
    schedule_cfg = cfg.get("schedule", {})
    scheduler = BlockingScheduler(timezone="UTC")
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

    prices_interval = schedule_cfg.get("prices_interval_minutes", 20)
    scheduler.add_job(
        _safe_run, IntervalTrigger(minutes=prices_interval), args=[run_prices_once, cfg, "prices"],
        id="prices", max_instances=1, misfire_grace_time=300,
    )

    global _league_start_mode_active
    meta_trigger, _league_start_mode_active = _compute_meta_trigger(schedule_cfg)
    scheduler.add_job(
        _safe_run, meta_trigger, args=[run_meta_once, cfg, "meta"],
        id="meta", max_instances=1, misfire_grace_time=3600,
    )
    _update_league_start_mode_status(publisher, _league_start_mode_active)

    cleanup_hour = schedule_cfg.get("cleanup_hour_thailand", 4)
    scheduler.add_job(
        _safe_run, CronTrigger(hour=cleanup_hour, minute=0, timezone="Asia/Bangkok"),
        args=[run_cleanup_once, cfg, "cleanup"], id="cleanup", max_instances=1, misfire_grace_time=3600,
    )

    news_interval = schedule_cfg.get("news_interval_minutes", 5)
    scheduler.add_job(
        _safe_run, IntervalTrigger(minutes=news_interval), args=[run_news_once, cfg, "news"],
        id="news", max_instances=1, misfire_grace_time=120,
    )

    # No healthcheck ping for this one -- it's scheduler bookkeeping, not a
    # data-fetching job; heartbeat.yml's existing META_MAX_AGE_HOURS=26
    # tolerance already covers the meta job itself at any active frequency.
    scheduler.add_job(
        _check_league_start_mode, IntervalTrigger(hours=1), args=[scheduler, cfg, publisher],
        id="league_start_mode_check", max_instances=1, misfire_grace_time=1800,
    )

    log.info(
        "scheduler: starting (prices every %dmin, meta %s [league_start_mode %s], cleanup daily %02d:00 ICT, news every %dmin)",
        prices_interval, meta_trigger, "active" if _league_start_mode_active else "inactive", cleanup_hour, news_interval,
    )
    scheduler.start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-prices-once", action="store_true")
    parser.add_argument("--run-meta-once", action="store_true")
    parser.add_argument("--run-cleanup-once", action="store_true")
    parser.add_argument("--run-news-once", action="store_true")
    parser.add_argument("--serve", action="store_true", help="Run the persistent scheduler (prices+meta+cleanup+news)")
    args = parser.parse_args()

    cfg = load_config()
    _setup_logging(cfg)

    if args.run_prices_once:
        run_prices_once(cfg)
    elif args.run_meta_once:
        run_meta_once(cfg)
    elif args.run_cleanup_once:
        run_cleanup_once(cfg)
    elif args.run_news_once:
        run_news_once(cfg)
    elif args.serve:
        run_scheduler(cfg)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
