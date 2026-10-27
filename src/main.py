"""Entry point.

    python -m src.main --run-prices-once
    python -m src.main --run-meta-once [--game poe1|poe2 ...] [--only slot|skill]
    python -m src.main --run-cleanup-once
    python -m src.main --run-news-once
    python -m src.main --run-gems-once
    python -m src.main --run-aliases-once
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.fetchers import deals, news, ninja_prices, scout_prices
from src.fetchers.league_dates import LeagueDatesFetcher
from src.fetchers.league_detector import LeagueDetector
from src.fetchers.ninja_builds import NinjaBuildsFetcher
from src.fetchers.ninja_chars import NinjaCharsFetcher, item_detail_from_dict
from src.fetchers.ninja_prices import NinjaPricesFetcher
from src.fetchers.scout_prices import ScoutPricesFetcher
from src.processing.aggregator import (
    aggregate_jewels,
    aggregate_slot,
    compute_archetype_distribution,
    reprocess_chars_to_skill_mod_counts,
)
from src.processing.archetyper import Archetyper
from src.processing.archive_export import DEFAULT_ARCHIVE_DIR, export_league_archive
from src.processing.archive_runner import export_league_archive_streaming
from src.processing.char_cache import SnapshotProvider, StoreBackedCharacterCache
from src.processing.skill_collect import CollectConfig, run_skill_collection
from src.processing.skill_publish import (
    attach_skill_tags,
    build_skill_dictionary,
    build_skill_groups,
    build_skill_passives_schema,
    build_skills_index,
    iter_skill_mods,
    NonContiguousRows,
)
from src.processing.rendering_aliases import valid_public as valid_public_aliases
from src.processing.skill_normalizer import SkillNormalizer
from src.processing.normalizer import ModNormalizer
from src.storage.local_db import COLLECTION_METHOD_LEGACY, DEFAULT_DB_PATH, LocalDb
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
# RePoE-derived item-text aliases (tools/refresh_rendering_aliases.py -> src/processing/rendering_aliases.py). Same
# two-hop reasoning as the stats cache: built in GitHub Actions, only ever downloaded by the VM.
META_ALIASES_BLOB_PATH_BY_GAME = {
    "poe2": "internal/rendering_aliases_poe2.json",
    "poe1": "internal/rendering_aliases_poe1.json",
}
# Must match tools/refresh_gems.py's TARGETS blob_paths exactly -- that's
# what actually stages these files (skill-meta phase 1). run_gems_once below
# republishes each one to _gems_blob_path(game), the real public path.
GEMS_STAGED_BLOB_PATH_BY_GAME = {
    "poe2": "internal/gems_normalized_poe2.json",
    "poe1": "internal/gems_normalized_poe1.json",
}


def _gems_blob_path(game: str) -> str:
    return f"{game}/gems.json"


# The PUBLIC rendering-alias contract (SPEC.md 8.12). tools/refresh_rendering_aliases.py stages it here (it may only
# write internal/); run_aliases_once copies it to the real public path with the VM's full-access credentials.
ALIASES_STAGED_BLOB_PATH_BY_GAME = {
    "poe2": "internal/aliases_public_poe2.json",
    "poe1": "internal/aliases_public_poe1.json",
}


def _aliases_blob_path(game: str) -> str:
    return f"{game}/aliases/latest.json"


# Skill-meta phase 2 (item 6): config-overridable, kept small here as the
# fallback when config.yaml's skill_meta.level_brackets is absent.
DEFAULT_LEVEL_BRACKETS: list[tuple[int, int]] = [(28, 65), (65, 75), (75, 90), (90, 100)]


def _level_brackets(cfg: dict[str, Any]) -> list[tuple[int, int]]:
    raw = cfg.get("skill_meta", {}).get("level_brackets")
    if not raw:
        return DEFAULT_LEVEL_BRACKETS
    return [tuple(b) for b in raw]


def _gems_url(cfg: dict[str, Any], game: str) -> str:
    return f"https://storage.googleapis.com/{cfg['firebase']['bucket']}/{_gems_blob_path(game)}"


def _meta_blob_path(game: str) -> str:
    return f"{game}/meta/latest.json"


def _meta_dated_blob_path(game: str, date_str: str) -> str:
    return f"{game}/meta/{date_str}.json"


def _passives_blob_path(game: str) -> str:
    return f"{game}/passives/latest.json"


def _skills_index_blob_path(game: str) -> str:
    return f"{game}/skills/index.json"


def _skill_dictionary_blob_path(game: str) -> str:
    return f"{game}/skills/dictionary.json"


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


_ping_unconfigured_logged: set[str] = set()


def _ping_healthcheck(http: HttpClient, url: Optional[str], job_name: str) -> None:
    """Layer-1 dead-man-switch ping (healthchecks.io or compatible) -- fired
    only after this job's Firebase publish + readback-verify has already
    succeeded, not just "the job finished" (see CLAUDE.md). `url` is
    optional; blank/missing means this layer is off for that job, not an
    error. A failed ping must never fail the job itself."""
    if not url:
        if job_name not in _ping_unconfigured_logged:       # once per process: news pings every 5 min
            _ping_unconfigured_logged.add(job_name)
            log.warning("%s job: no healthcheck ping URL configured (env var unset?) -- this job is NOT being watched", job_name)
        return
    try:
        http.get_bytes(url)
        log.info("%s job: healthcheck ping sent", job_name)            # never the URL: it is a secret
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


def run_gems_once(cfg: dict[str, Any]) -> dict[str, Any]:
    """Republishes `{game}/gems.json` (skill/support gem name+metadata
    table) from the `internal/gems_normalized_{game}.json` staging copy that
    `refresh-stats.yml`'s GitHub Actions job writes weekly via
    `tools/refresh_gems.py`.

    Two-hop publish, same reason `internal/stats_cache.json` exists (SPEC.md
    8.3): building gems.json needs `trade(2)/data/items`, which GGG blocks
    from GCP IPs -- so GitHub Actions (using its own `internal/`-scoped
    `stats-refresh` service account, CLAUDE.md) does the fetch+normalize and
    stages the *already-normalized, already-filtered* result. This job just
    copies that staged file to the real public path using the VM's
    full-access credentials, which the scoped GitHub Actions service account
    deliberately cannot write to directly. The staged file is already small
    (PoE1's ~41MB raw RePoE response is parsed down to a lean gem list on
    GitHub's runner, never here -- see the VM RAM budget note in CLAUDE.md),
    so this hop is a cheap Firebase-to-Firebase copy, no external API calls.

    Skips a game (stale-while-error at the whole-file level, same as
    refresh_stats.py) if its staging copy hasn't been populated yet -- e.g.
    before refresh-stats.yml has run even once since a game was added (same
    "Deploy gotcha" class of issue as 8.3): logs a warning rather than
    failing the job, and the previously-published public copy (if any) is
    left untouched."""
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

    per_game: dict[str, dict[str, Any]] = {}
    overall_ok = True
    for game in cfg.get("game_versions", ["poe2"]):
        staged_path = GEMS_STAGED_BLOB_PATH_BY_GAME.get(game)
        if staged_path is None:
            continue
        public_path = _gems_blob_path(game)

        staged = publisher.fetch_previous(staged_path)
        if staged is None:
            overall_ok = False
            log.warning("gems job(%s): staging copy not found yet (%s) -- skipping this cycle", game, staged_path)
            per_game[game] = {"ok": False, "reason": "no staged data"}
            continue

        try:
            publisher.publish(staged, public_path, cache_control="public, max-age=3600")
        except Exception as e:
            overall_ok = False
            log.error("gems job(%s): publish failed: %s", game, e, exc_info=True)
            per_game[game] = {"ok": False, "reason": str(e)}
            continue

        count = len(staged.get("gems", []))
        log.info("gems job(%s): republished %d gems -> %s", game, count, public_path)
        per_game[game] = {"ok": True, "count": count, "generated_at": staged.get("generated_at")}

    return {"ok": overall_ok, "games": per_game}


def run_aliases_once(cfg: dict[str, Any]) -> dict[str, Any]:
    """Republishes `{game}/aliases/latest.json` (the rendering-alias contract the PoE Checker reads, SPEC.md 8.12)
    from `internal/aliases_public_{game}.json`, which `refresh-stats.yml` stages weekly. Same two-hop shape and
    reason as `run_gems_once`: the Actions service account is IAM-limited to `internal/`. Never builds anything:
    a staged file that is missing or fails its shape check is skipped with a log line and the previously
    published public file stays exactly as it was. Cache-Control follows `dictionary.json` (24h, config
    `aliases.cache_max_age_seconds`): the table only changes when RePoE does, i.e. weekly."""
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))
    max_age = cfg.get("aliases", {}).get("cache_max_age_seconds", 86400)

    per_game: dict[str, dict[str, Any]] = {}
    overall_ok = True
    for game in cfg.get("game_versions", ["poe2"]):
        staged_path = ALIASES_STAGED_BLOB_PATH_BY_GAME.get(game)
        if staged_path is None:
            continue
        staged = publisher.fetch_previous(staged_path)
        if staged is None:
            overall_ok = False
            log.warning("aliases job(%s): staging copy not found yet (%s) -- skipping this cycle", game, staged_path)
            per_game[game] = {"ok": False, "reason": "no staged data"}
            continue
        if not valid_public_aliases(staged) or staged.get("game") != game:
            overall_ok = False
            log.error("aliases job(%s): staged file %s fails the contract shape check -- NOT publishing", game, staged_path)
            per_game[game] = {"ok": False, "reason": "invalid staged file"}
            continue
        try:
            publisher.publish(staged, _aliases_blob_path(game), cache_control=f"public, max-age={max_age}", compact=True)
        except Exception as e:
            overall_ok = False
            log.error("aliases job(%s): publish failed: %s", game, e, exc_info=True)
            per_game[game] = {"ok": False, "reason": str(e)}
            continue
        log.info("aliases job(%s): republished %d aliases, %d stat groups -> %s", game, len(staged["aliases"]),
                 len(staged["stat_groups"]), _aliases_blob_path(game))
        per_game[game] = {"ok": True, "aliases": len(staged["aliases"]), "generated_at": staged.get("generated_at")}
    return {"ok": overall_ok, "games": per_game}


def run_reprocess_char_items(cfg: dict[str, Any]) -> dict[str, Any]:
    """`--reprocess-char-items` (skill-meta phase 2, item 13): recomputes
    `skill_mod_counts` from already-stored `char_items` rows, entirely
    offline -- no poe.ninja calls. Exists so a change to `{game}/gems.json`'s
    normalization, or to `skill_meta.level_brackets`, can be applied
    retroactively across the whole 90-day `char_items` retention window
    without re-fetching anything (item 5's "ไม่ต้องยิง ninja เพิ่ม").

    Only rows with `main_skill` populated are usable -- that column didn't
    exist before this feature shipped, so historical rows from earlier than
    this deploy have no raw skill name to recover (see CLAUDE.md; accepted
    as a known gap rather than attempting an inaccurate re-fetch-based
    backfill, which would silently mislabel respecced characters).

    Idempotent per snapshot: each affected snapshot's existing
    `skill_mod_counts` rows are deleted and replaced, not accumulated, so
    running this again (e.g. after a further gems.json fix) never
    double-counts."""
    firebase_cfg = cfg["firebase"]
    http = _build_http_client(cfg)
    archetyper = Archetyper()
    level_brackets = _level_brackets(cfg)

    per_game: dict[str, Any] = {}
    db = LocalDb()
    try:
        for game in cfg.get("game_versions", ["poe2"]):
            normalizer = ModNormalizer(
                http, game=game,
                cache_ttl_seconds=cfg.get("ggg", {}).get("stats_cache_ttl_seconds", 7 * 24 * 60 * 60),
                firebase_stats_url=f"https://storage.googleapis.com/{firebase_cfg['bucket']}/{META_STATS_CACHE_BLOB_PATH_BY_GAME[game]}",
                firebase_aliases_url=f"https://storage.googleapis.com/{firebase_cfg['bucket']}/{META_ALIASES_BLOB_PATH_BY_GAME[game]}",
            )
            normalizer.load()
            skill_normalizer = SkillNormalizer(http, game=game, gems_url=_gems_url(cfg, game))
            skill_normalizer.load()

            rows = db.iter_char_items_with_main_skill(game)
            by_snapshot: dict[int, list[tuple[Optional[str], int, Any]]] = {}
            for _char_item_id, snapshot_id, main_skill, level, items_raw in rows:
                by_snapshot.setdefault(snapshot_id, []).append(
                    (main_skill, level, [item_detail_from_dict(i) for i in items_raw])
                )

            total_rows_written = 0
            for snapshot_id, chars in by_snapshot.items():
                skill_counts = reprocess_chars_to_skill_mod_counts(
                    chars, normalizer=normalizer, skill_normalizer=skill_normalizer,
                    archetyper=archetyper, level_brackets=level_brackets,
                )
                db.delete_skill_mod_counts_for_snapshot(snapshot_id)
                days_since_league_start = db.get_snapshot_days_since_league_start(snapshot_id)
                db.insert_skill_mod_counts(
                    snapshot_id, game, days_since_league_start, [dataclasses.asdict(m) for m in skill_counts]
                )
                total_rows_written += len(skill_counts)

            per_game[game] = {
                "char_items_rows": len(rows),
                "snapshots_reprocessed": len(by_snapshot),
                "skill_mod_counts_rows_written": total_rows_written,
                "unmapped_skill_names": dict(skill_normalizer.unmapped_names),
            }
    finally:
        db.close()

    log.info("reprocess_char_items: %s", per_game)
    return per_game


def _detect_league_and_dates(http: HttpClient, cfg: dict[str, Any], game: str):
    """League name, its start/end dates, and `days_since_league_start` (None
    only when no start date could be established -- see
    `LeagueDatesFetcher._find_start`)."""
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
    return league, league_dates, days_since_league_start


def _run_meta_for_game(http: HttpClient, publisher: PricesPublisher, cfg: dict[str, Any], game: str) -> dict[str, Any]:
    """The legacy per-slot job: top-100 characters per equipment slot ->
    `mod_counts` (archetype-based) -> meta/latest.json. Kept because
    consumers still read that file; a temporary bridge -- see SPEC.md
    "Skill-centric collection". It no longer produces any skill- or
    passive-level data (that moved to `_run_skill_meta_for_game`), and its
    first-gem `archetype_hint` is used for archetype bucketing only.

    Shares the persistent character cache with the skill-centric job: a
    character fetched by either is reused (same league + level, younger than
    `skill_meta.collect.reuse_max_age_days`) instead of refetched."""
    job_start = time.monotonic()
    league, league_dates, days_since_league_start = _detect_league_and_dates(http, cfg, game)

    collect_cfg = CollectConfig.from_cfg(cfg)
    builds_fetcher = NinjaBuildsFetcher(http, game=game)
    snapshots = SnapshotProvider(
        lambda: builds_fetcher.resolve_snapshot(league), refresh_seconds=collect_cfg.snapshot_refresh_seconds
    )
    snapshot = snapshots.get()

    by_slot, per_slot_counts = builds_fetcher.fetch_all(snapshot)
    log.info("meta job(%s): fetched %d slot lists: %s", game, len(by_slot), per_slot_counts)

    firebase_bucket = cfg["firebase"]["bucket"]
    normalizer = ModNormalizer(
        http,
        game=game,
        cache_ttl_seconds=cfg.get("ggg", {}).get("stats_cache_ttl_seconds", 7 * 24 * 60 * 60),
        firebase_stats_url=f"https://storage.googleapis.com/{firebase_bucket}/{META_STATS_CACHE_BLOB_PATH_BY_GAME[game]}",
        firebase_aliases_url=f"https://storage.googleapis.com/{firebase_bucket}/{META_ALIASES_BLOB_PATH_BY_GAME[game]}",
    )
    normalizer.load()
    log.info("meta job(%s): stats loaded from %s (fetched_at=%s)", game, normalizer.stats_source, normalizer.stats_fetched_at)
    archetyper = Archetyper()

    level_by_key = {(r.account, r.name): r.level for refs in by_slot.values() for r in refs}
    all_mod_counts = []
    per_slot_sample: dict[str, int] = {}

    db = LocalDb()
    try:
        cache = StoreBackedCharacterCache(
            db, NinjaCharsFetcher(http, game=game), snapshots, game, league,
            max_age_days=collect_cfg.reuse_max_age_days, memoize=True,
        )

        def get_detail(account: str, name: str):
            return cache.get(account, name, level_by_key.get((account, name)))

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
        log.info(
            "meta job(%s): characters -- fetched %d NEW, reused %d from the shared cache, %d unique loaded, %d failed",
            game, cache.fetched_count, cache.reused_count, cache.unique_ok_count, cache.failed_count,
        )

        _archive_previous_league_if_changed(db, publisher, game, league, archive_enabled=_archive_enabled(cfg), cfg=cfg)

        snapshot_id = db.insert_snapshot(
            game=game,
            league=league,
            days_since_league_start=days_since_league_start,
            sample_total=sum(per_slot_counts.values()),
            unique_characters_fetched=cache.unique_ok_count,
            fetch_failed=cache.failed_count,
            collection_method=COLLECTION_METHOD_LEGACY,
        )
        for (account, name), ref in unique_refs_by_key.items():
            detail = get_detail(account, name)
            if detail is None:
                continue
            archetype = archetyper.classify(
                detail.archetype_hint, next((i.base_type for i in detail.items if i.slot == "weapon"), None)
            )
            db.insert_char_item(
                snapshot_id, account, name, detail.level, detail.char_class, archetype,
                [dataclasses.asdict(i) for i in detail.items],
                main_skill=None,  # never persist the first-gem guess as a "main skill"
                passive_selection=list(detail.passive_selection),
            )
        db.insert_mod_counts(snapshot_id, [dataclasses.asdict(m) for m in all_mod_counts])
        db.commit()
    finally:
        db.close()

    data = build_meta_schema(
        league=league,
        days_since_league_start=days_since_league_start,
        per_slot_sample=per_slot_sample,
        unique_characters_fetched=cache.unique_ok_count,
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
    normalizer.log_unmapped_summary()
    peak_rss_mb = _peak_memory_mb()
    log.info("meta job(%s): peak RSS = %s MB", game, peak_rss_mb if peak_rss_mb is not None else "unknown (non-Unix)")

    return {
        "data": data,
        "end_at": league_dates.end_at,
        "summary": {
            "duration_seconds": round(time.monotonic() - job_start, 1),
            "sample_total": sum(per_slot_counts.values()),
            "unique_characters_fetched": cache.unique_ok_count,
            "characters_fetched_new": cache.fetched_count,
            "characters_reused": cache.reused_count,
            "dedupe_savings": sum(per_slot_counts.values()) - cache.unique_ok_count,
            "characters_failed": cache.failed_count,
            "mods_processed": normalizer.processed_count,
            "unmapped_mod_pct": unmapped_pct,
            "peak_rss_mb": peak_rss_mb,
            "stats_source": normalizer.stats_source,
            "stats_cache_fetched_at": normalizer.stats_fetched_at,
        },
    }


def _run_skill_meta_for_game(
    http: HttpClient, publisher: PricesPublisher, cfg: dict[str, Any], game: str,
) -> dict[str, Any]:
    """The skill-centric job: top-100 characters per poe.ninja skill list
    (main, and spirit in PoE2), checkpointed and resumable -- see
    `src/processing/skill_collect.py`. Publishes `{game}/skills/*` and the new
    `{game}/passives/latest.json` immediately after a successful collection
    (see `publish_skill_meta`), reusing the same `normalizer` (no extra
    fetch)."""
    league, _league_dates, days_since_league_start = _detect_league_and_dates(http, cfg, game)

    collect_cfg = CollectConfig.from_cfg(cfg)
    builds_fetcher = NinjaBuildsFetcher(http, game=game)
    snapshots = SnapshotProvider(
        lambda: builds_fetcher.resolve_snapshot(league), refresh_seconds=collect_cfg.snapshot_refresh_seconds
    )
    firebase_bucket = cfg["firebase"]["bucket"]
    normalizer = ModNormalizer(
        http,
        game=game,
        cache_ttl_seconds=cfg.get("ggg", {}).get("stats_cache_ttl_seconds", 7 * 24 * 60 * 60),
        firebase_stats_url=f"https://storage.googleapis.com/{firebase_bucket}/{META_STATS_CACHE_BLOB_PATH_BY_GAME[game]}",
        firebase_aliases_url=f"https://storage.googleapis.com/{firebase_bucket}/{META_ALIASES_BLOB_PATH_BY_GAME[game]}",
    )
    normalizer.load()
    log.info("skill meta job(%s): stats loaded from %s", game, normalizer.stats_source)

    db = LocalDb()
    try:
        _archive_previous_league_if_changed(db, publisher, game, league, archive_enabled=_archive_enabled(cfg), cfg=cfg)
        cache = StoreBackedCharacterCache(
            db, NinjaCharsFetcher(http, game=game), snapshots, game, league,
            max_age_days=collect_cfg.reuse_max_age_days, memoize=False,
        )
        stats = run_skill_collection(
            game=game, league=league, days_since_league_start=days_since_league_start,
            builds_fetcher=builds_fetcher, snapshots=snapshots, cache=cache, db=db,
            normalizer=normalizer, level_brackets=_level_brackets(cfg), config=collect_cfg,
        )
        publish_result = publish_skill_meta(db, publisher, game, cfg, normalizer, http)
        stats["publish"] = publish_result
    finally:
        db.close()
    normalizer.log_unmapped_summary()
    stats["peak_rss_mb"] = _peak_memory_mb()
    log.info("skill meta job(%s): peak RSS = %s MB", game, stats["peak_rss_mb"])
    return stats


def _load_gems_for_tags(http: HttpClient, cfg: dict[str, Any], game: str) -> list[dict[str, Any]]:
    """Best-effort load of the published `{game}/gems.json` for the tags join
    (see `attach_skill_tags`). Never raises -- a load failure degrades to
    `tags: []` for every skill this publish, same "never crash the job on a
    bad/empty response" convention as `skill_normalizer.py`'s own gems.json
    load, rather than blocking the whole skill-meta publish on it."""
    try:
        data = http.get_json(_gems_url(cfg, game))
        gems = data.get("gems")
        if not isinstance(gems, list) or not gems:
            raise ValueError(f"unexpected gems.json shape: no non-empty 'gems' list")
        return gems
    except Exception as e:
        log.error(
            "skill publish(%s): could not load gems.json for tags join (%s) -- "
            "publishing with tags=[] for every skill this run", game, e,
        )
        return []


def publish_skill_meta(
    db: LocalDb, publisher: PricesPublisher, game: str, cfg: dict[str, Any], normalizer: ModNormalizer,
    http: HttpClient,
) -> dict[str, Any]:
    """Publishes from whatever skill-centric run most recently *finished*
    (`skill_runs.status = 'finished'`) for `game` -- not necessarily the one
    that was just collected: this also lets `--publish-skill-meta` republish
    from an already-completed run without recollecting (e.g. after a publish
    bug fix, the same pattern `run_gems_once` uses for its staged copy).

    Publishes, all under `skill_meta.publish` config: `{game}/skills/index.json`
    (every skill, `populations` shown even when a skill's base is thin -- never
    filtered out, see skill_publish.py), one `{game}/skills/{type}/{slug}.json`
    per skill (compact JSON), a shared `{game}/skills/dictionary.json`
    (stat_id -> template, not duplicated into every skill file), and the new
    `{game}/passives/latest.json` (schema_version bumped, full shape
    replacement -- see SPEC.md 8.9/CLAUDE.md)."""
    run = db.latest_finished_skill_run(game)
    if run is None:
        log.warning("skill publish(%s): no finished skill-centric run yet -- nothing to publish", game)
        return {"ok": False, "reason": "no finished run"}

    publish_cfg = cfg.get("skill_meta", {}).get("publish", {})
    min_usage_pct = publish_cfg.get("min_usage_pct", 0.1)
    index_max_age = publish_cfg.get("index_cache_max_age_seconds", 3600)
    skill_max_age = publish_cfg.get("skill_file_cache_max_age_seconds", 21600)
    dict_max_age = publish_cfg.get("dictionary_cache_max_age_seconds", 86400)

    snapshot_id, league, days = run["snapshot_id"], run["league"], run["days_since_league_start"]
    # The stat rows are the big table (PoE2: 340k rows). They are NOT loaded here any more: the groups are built from
    # the small populations/passives rows (they decide which skills exist, their slugs, index and passives), and each
    # skill's mods are streamed in below, one skill at a time (2026-10-19: fetchall + a dict per row held ~190MB
    # (PoE1) / ~300MB (PoE2) and took the hub to 439MB of a 460MB cap).
    passive_rows = db.iter_skill_passive_counts_for_snapshot(snapshot_id)
    population_rows = db.iter_skill_populations_for_snapshot(snapshot_id)
    groups = build_skill_groups((), passive_rows, population_rows, min_usage_pct)
    population_total = db.get_skill_run_population_total(run["run_id"])

    gems = _load_gems_for_tags(http, cfg, game)
    groups, tags_report = attach_skill_tags(groups, gems)

    index_data = build_skills_index(game, league, days, population_total, groups)
    publisher.publish(index_data, _skills_index_blob_path(game), cache_control=f"public, max-age={index_max_age}")

    def publish_skill_file(g: Any) -> None:
        publisher.publish(
            g.to_file(game, league, days), g.path(game), cache_control=f"public, max-age={skill_max_age}", compact=True,
        )

    by_key = {(g.skill_type, g.skill): g for g in groups}
    published_keys: set[tuple[str, str]] = set()
    try:
        for key, mods in iter_skill_mods(db.stream_skill_stat_counts_for_snapshot(snapshot_id), min_usage_pct):
            g = by_key.get(key)
            if g is None:          # rows for a skill with no population row: never published (same as before)
                continue
            publish_skill_file(dataclasses.replace(g, mods=mods))
            published_keys.add(key)
    except NonContiguousRows as e:
        # Cannot happen with the current writer; if it ever does, publish from the whole table in memory (the old,
        # memory-hungry path) rather than ever publishing a skill with only part of its rows.
        log.error("skill publish(%s): stat rows not contiguous per skill (%s) -- falling back to the buffered path", game, e)
        full = build_skill_groups(db.iter_skill_stat_counts_for_snapshot(snapshot_id), passive_rows, population_rows, min_usage_pct)
        full, _ = attach_skill_tags(full, gems)
        groups = full
        by_key = {(g.skill_type, g.skill): g for g in groups}
        published_keys = set()
    for g in groups:                       # skills with no (qualifying) mod rows still get their file
        if (g.skill_type, g.skill) not in published_keys:
            publish_skill_file(g)

    dictionary_data = build_skill_dictionary(game, normalizer.stat_dictionary())
    publisher.publish(dictionary_data, _skill_dictionary_blob_path(game), cache_control=f"public, max-age={dict_max_age}")

    passives_data = build_skill_passives_schema(game, league, days, groups)
    publisher.publish(passives_data, _passives_blob_path(game), cache_control="public, max-age=3600")

    log.info(
        "skill publish(%s): run %d (snapshot %d) -- %d skills (%d main, %d spirit), index+dictionary+passives; "
        "tags joined %d/%d (%d ambiguous, resolved via intersection)",
        game, run["run_id"], snapshot_id, len(groups),
        sum(1 for g in groups if g.skill_type == "main"), sum(1 for g in groups if g.skill_type == "spirit"),
        tags_report["joined"], len(groups), tags_report["ambiguous_count"],
    )
    return {
        "ok": True, "run_id": run["run_id"], "snapshot_id": snapshot_id, "league": league,
        "skills_published": len(groups),
        "main_skills": sum(1 for g in groups if g.skill_type == "main"),
        "spirit_skills": sum(1 for g in groups if g.skill_type == "spirit"),
        "tags": tags_report,
    }


def run_publish_skill_meta_once(cfg: dict[str, Any], games: list[str]) -> dict[str, dict[str, Any]]:
    """`--publish-skill-meta --game G [--game G2 ...]`: (re)publish from the
    latest finished skill-centric run for each game, without recollecting --
    the escape hatch for shipping a publish-side fix without repeating an
    8-10h collection. Not guarded by job_lock: it only reads SQLite + a cheap
    ModNormalizer load and writes to Firebase, none of the rate-limited
    poe.ninja calls job_lock protects against."""
    http = _build_http_client(cfg)
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))
    results: dict[str, dict[str, Any]] = {}
    db = LocalDb()
    try:
        for game in games:
            normalizer = ModNormalizer(
                http, game=game,
                cache_ttl_seconds=cfg.get("ggg", {}).get("stats_cache_ttl_seconds", 7 * 24 * 60 * 60),
                firebase_stats_url=f"https://storage.googleapis.com/{firebase_cfg['bucket']}/{META_STATS_CACHE_BLOB_PATH_BY_GAME[game]}",
                firebase_aliases_url=f"https://storage.googleapis.com/{firebase_cfg['bucket']}/{META_ALIASES_BLOB_PATH_BY_GAME[game]}",
            )
            normalizer.load()
            results[game] = publish_skill_meta(db, publisher, game, cfg, normalizer, http)
    finally:
        db.close()
    log.info("publish_skill_meta: %s", results)
    return results


META_PARTS = ("slot", "skill")


def run_meta_once(
    cfg: dict[str, Any], games: Optional[list[str]] = None, parts: Optional[tuple[str, ...]] = None,
) -> dict[str, dict[str, Any]]:
    """Runs, per game (default: every configured game, one after the other):
    the legacy per-slot job ("slot" part -> meta/latest.json) and then the
    skill-centric job ("skill" part -> SQLite; resumable). `games` / `parts`
    restrict that -- the multi-hour first skill-centric run is launched as
    `--run-meta-once --game poe1 --only skill`. One game's failure never
    blocks or corrupts another's; the single healthchecks.io ping fires only
    when the full default run (every game, slot part) succeeded.

    Guarded by job_lock so the scheduler's own cron and a manually launched
    `--run-meta-once` (or two overlapping manual runs) can never race the
    same rate-limited hosts concurrently -- both call this exact function,
    so locking here covers both callers. A second attempt while one is
    already running skips cleanly rather than colliding (see CLAUDE.md's
    2026-07-15 incident, where exactly this collision stalled poe.ninja
    requests for hours via duplicate, uncoordinated retries). The lock is
    held for the whole multi-hour skill-centric run too, so the scheduler's
    ticks simply skip while it works."""
    try:
        with job_lock.acquire("meta"):
            return _run_meta_once_locked(cfg, games, parts)
    except job_lock.JobAlreadyRunning as e:
        log.warning(
            "meta job: skip -- previous run still active (pid=%s, started_at=%s)",
            e.holder.pid, e.holder.started_at,
        )
        return {}


def _run_meta_once_locked(
    cfg: dict[str, Any], games: Optional[list[str]] = None, parts: Optional[tuple[str, ...]] = None,
) -> dict[str, dict[str, Any]]:
    http = _build_http_client(cfg)
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

    configured = list(cfg.get("game_versions", ["poe2"]))
    # When `games` is given explicitly, iterate in ITS order (filtered to
    # configured membership) -- e.g. the scheduled skill-centric job always
    # passes games=["poe1", "poe2"] to guarantee PoE1 runs before PoE2
    # (approved 2026-09-22: never in parallel, PoE1 first), regardless of
    # `game_versions`' own order in config.yaml. `games=None` keeps the
    # original behavior of following `configured`'s order.
    selected = configured if games is None else [g for g in games if g in configured]
    unknown = [g for g in (games or []) if g not in configured]
    if unknown:
        raise ValueError(f"meta job: game(s) {unknown} not in configured game_versions {configured}")
    explicit_parts = bool(parts)
    parts = tuple(parts) if parts else META_PARTS
    if not explicit_parts and not cfg.get("skill_meta", {}).get("collect", {}).get("scheduled", False):
        # Off by default so a deploy can never silently start the multi-hour
        # first skill-centric run; `--only skill` (explicit) always runs.
        parts = tuple(p for p in parts if p != "skill")
        log.info("meta job: skill-centric part is not scheduled (skill_meta.collect.scheduled=false); run it with --only skill")

    _mark_job_running(publisher, "meta")
    try:
        results: dict[str, dict[str, Any]] = {}
        skill_results: dict[str, dict[str, Any]] = {}
        for game in selected:
            if "slot" in parts:
                try:
                    results[game] = _run_meta_for_game(http, publisher, cfg, game)
                except Exception as e:
                    log.error("meta job(%s): failed entirely: %s", game, e, exc_info=True)
            if "skill" in parts:
                try:
                    skill_results[game] = _run_skill_meta_for_game(http, publisher, cfg, game)
                except Exception as e:
                    log.error("skill meta job(%s): failed: %s", game, e, exc_info=True)

        if "slot" not in parts or set(selected) != set(configured):
            log.info("meta job: partial run (games=%s, parts=%s) -- no healthcheck ping / status update", selected, parts)
        elif _all_games_got_real_characters(cfg, results):
            _ping_healthcheck(http, cfg.get("healthchecks", {}).get("meta_ping_url"), "meta")
        else:
            log.warning(
                "meta job: skipping healthcheck ping -- at least one configured game got 0 characters "
                "or failed entirely this cycle (sample_total by game: %s)",
                {g: r.get("summary", {}).get("sample_total") for g, r in results.items()},
            )
        if results:
            _update_meta_status(publisher, results)

        # Skill-centric ping (approved 2026-09-22): independent of the slot
        # ping above -- its own healthchecks.io check, fires only when every
        # configured game ran (this exact call, not some earlier partial one)
        # and got a nonzero character count, same "never ping on a partial/
        # empty cycle" rule as the slot job's own ping (see
        # _all_games_got_real_characters's docstring for why that rule
        # exists at all).
        if "skill" in parts:
            if set(selected) != set(configured):
                log.info("skill meta job: partial run (games=%s) -- no healthcheck ping", selected)
            elif _all_games_got_real_skill_characters(cfg, skill_results):
                _ping_healthcheck(http, cfg.get("healthchecks", {}).get("skill_meta_ping_url"), "skill_meta")
            else:
                log.warning(
                    "skill meta job: skipping healthcheck ping -- at least one configured game got 0 characters "
                    "or failed entirely this cycle (characters_distinct by game: %s)",
                    {g: r.get("characters_distinct") for g, r in skill_results.items()},
                )

        return {game: r["data"] for game, r in results.items()}
    finally:
        _mark_job_finished(publisher, "meta")


def _all_games_got_real_characters(cfg: dict[str, Any], results: dict[str, dict[str, Any]]) -> bool:
    """The Layer-1 ping must never fire on a cycle where any configured game
    returned zero characters -- exactly the shape the 2026-08-04 ninja_builds
    schema break took: the job "succeeded" (published, pinged) every single
    cycle for ~43 days while the actual character sample was silently empty,
    so freshness/ping-success alone never caught it (see CLAUDE.md). A game
    missing from `results` entirely (its `_run_meta_for_game` call raised)
    also fails this check, same as a present-but-empty one."""
    configured = cfg.get("game_versions", ["poe2"])
    if set(results) != set(configured):
        return False
    return all(r.get("summary", {}).get("sample_total", 0) > 0 for r in results.values())


def _all_games_got_real_skill_characters(cfg: dict[str, Any], skill_results: dict[str, dict[str, Any]]) -> bool:
    """Same rule as `_all_games_got_real_characters`, for the skill-centric
    ping: a game missing entirely (its `_run_skill_meta_for_game` call
    raised) or present with zero distinct characters both fail the check."""
    configured = cfg.get("game_versions", ["poe2"])
    if set(skill_results) != set(configured):
        return False
    return all(r.get("characters_distinct", 0) > 0 for r in skill_results.values())


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


def _clear_stale_running_jobs(publisher: PricesPublisher) -> None:
    """Drops `status["running_jobs"]` entries whose pid is no longer alive.
    `_mark_job_finished` never runs when a process is OOM-killed or the VM
    is reset mid-job, so the entry used to stay forever (2026-10-15: a
    `meta` entry for the dead pid 1164340 outlived the reset by days).
    Called once at scheduler startup, when no job of THIS process can be
    running yet, so only another process's live entry (e.g. a manual
    `--run-meta-once`) is kept. Best-effort, never fails startup."""
    try:
        status = publisher.fetch_previous(STATUS_BLOB_PATH) or {}
        running = status.get("running_jobs") or {}
        stale = {name: info for name, info in running.items()
                 if not isinstance(info, dict) or not isinstance(info.get("pid"), int)
                 or info["pid"] == os.getpid() or not job_lock._pid_alive(info["pid"])}
        if not stale:
            return
        for name in stale:
            running.pop(name, None)
        status["running_jobs"] = running
        publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")
        log.warning("scheduler: cleared stale running_jobs entries %s (their processes are gone)", sorted(stale))
    except Exception as e:
        log.warning("could not clear stale running_jobs from status.json: %s", e)


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


def _archive_enabled(cfg: dict[str, Any]) -> bool:
    """`archive.enabled` in config.yaml; absent means enabled, so a config
    that never mentions it keeps the original behavior."""
    return bool(cfg.get("archive", {}).get("enabled", True))


def _archive_disabled_reason(cfg: dict[str, Any]) -> str:
    return str(cfg.get("archive", {}).get("disabled_reason") or "archive.enabled is false")


def _publish_archive_status(
    publisher: PricesPublisher, cfg: dict[str, Any], *, skipped_at: Optional[str] = None,
    unarchived_league: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """`status["archive"]`: whether the archive export is switched off and
    why, so a disabled archive is visible in status.json instead of just
    silently absent (the 2026-10-15 OOM fix disabled it on purpose; this is
    what stops it staying off unnoticed). Best-effort -- never fails a job.
    Returns the `archive` dict it wrote ({} if the status write failed)."""
    archive_status: dict[str, Any] = {}
    try:
        status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
        previous = status.get("archive") if isinstance(status.get("archive"), dict) else {}
        if _archive_enabled(cfg):
            archive_status = {"enabled": True}
        else:
            archive_status = {
                "enabled": False,
                "reason": _archive_disabled_reason(cfg),
                "last_skipped_at": skipped_at or previous.get("last_skipped_at"),
                "unarchived_league_changes": list(previous.get("unarchived_league_changes", [])),
            }
            if unarchived_league and not any(
                e.get("game") == unarchived_league["game"] and e.get("league") == unarchived_league["league"]
                for e in archive_status["unarchived_league_changes"]
            ):
                archive_status["unarchived_league_changes"].append(
                    {**unarchived_league, "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                )
        status["archive"] = archive_status
        publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")
    except Exception as e:
        log.warning("could not update status.json's archive flag: %s", e)
    return archive_status


def _warn_unarchived_leagues(archive_status: dict[str, Any]) -> None:
    """WARNING for every league that changed while the archive export was
    off. Called on EVERY skipped weekly cycle and at scheduler start, not
    just when the change is detected: unarchived rows age out under the
    skill-centric retention (45d) and are then gone for good."""
    for entry in archive_status.get("unarchived_league_changes", []):
        log.warning(
            "ARCHIVE GAP: %s league %r changed (detected %s) while the archive export was disabled and has NOT been "
            "archived -- its raw rows are deleted by retention ~45 days after they were collected. Re-enable the "
            "export or run `--export-league-archive %s:%s` before then.",
            entry.get("game"), entry.get("league"), entry.get("detected_at", "unknown"), entry.get("game"), entry.get("league"),
        )


def _archive_previous_league_if_changed(
    db: LocalDb, publisher: PricesPublisher, game: str, new_league: str, archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    archive_enabled: bool = True, cfg: Optional[dict[str, Any]] = None,
) -> None:
    """Called from `_run_meta_for_game` right before inserting the new
    snapshot: if this game's most-recently-recorded league differs from the
    one just detected, the old league has ended -- export its final archive
    now, while it's still trivial to find (skill-meta phase 5, item 21). A
    failure here must never block the meta run itself inserting its own
    data, only get logged loudly (the weekly safety-net job, or a manual
    `--export-league-archive`, can still catch it up later)."""
    previous_league = db.most_recent_league(game)
    if previous_league is None or previous_league == new_league:
        return
    if not archive_enabled:
        # Loud on purpose: skipping here means the old league's rows are
        # NOT archived and will age out under the skill-centric retention
        # (config.yaml `retention`, 45d) unless the export is run first.
        log.warning(
            "meta job(%s): league changed %r -> %r but the archive export is DISABLED (archive.enabled=false) -- "
            "the old league is NOT archived. Re-enable it or run `--export-league-archive %s:%s` before its raw "
            "rows age out under retention.", game, previous_league, new_league, game, previous_league,
        )
        if cfg is not None:
            _publish_archive_status(publisher, cfg, unarchived_league={"game": game, "league": previous_league})
        return
    log.info("meta job(%s): league changed %r -> %r, archiving the old league", game, previous_league, new_league)
    try:
        export_league_archive(db, game, previous_league, archive_dir=archive_dir, publisher=publisher)
    except Exception as e:
        log.error("meta job(%s): league-change archive export failed for %r: %s", game, previous_league, e, exc_info=True)


def run_archive_export_once(cfg: dict[str, Any], archive_dir: Path = DEFAULT_ARCHIVE_DIR) -> dict[str, Optional[str]]:
    """Weekly safety-net export (skill-meta phase 5, item 21): re-exports
    whichever league is currently active for each configured game, so a VM
    that disappears mid-league never loses more than a week of archived
    data. Idempotent (the export overwrites the same file) and cheap -- no
    poe.ninja calls, just reading the already-local SQLite tables. Does NOT
    touch leagues that have already ended; those were already archived once
    at the moment `_run_meta_for_game` detected the league change, and
    nothing about a closed league changes afterward. Returns
    {game: league_exported_or_None}."""
    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))
    if not _archive_enabled(cfg):
        log.warning(
            "archive job: SKIPPED this cycle -- the archive export is DISABLED (%s). No league archive is being "
            "written; status.json's `archive.enabled` is false until it is re-enabled.", _archive_disabled_reason(cfg),
        )
        _warn_unarchived_leagues(
            _publish_archive_status(publisher, cfg, skipped_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        )
        return {}
    results: dict[str, Optional[str]] = {}
    db = LocalDb()
    try:
        for game in cfg.get("game_versions", ["poe2"]):
            league = db.most_recent_league(game)
            if league is None:
                log.info("archive job(%s): no snapshots recorded yet, nothing to export", game)
                results[game] = None
                continue
            try:
                export_league_archive(db, game, league, archive_dir=archive_dir, publisher=publisher)
                results[game] = league
            except Exception as e:
                log.error("archive job(%s): export failed for league %r: %s", game, league, e, exc_info=True)
                results[game] = None
    finally:
        db.close()
    return results


def run_export_league_archive(
    cfg: dict[str, Any], game: str, league: str, archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    db_path: Path = DEFAULT_DB_PATH, extra_sources: Sequence[Path] = (), rebuild: bool = False, publish: bool = True,
) -> Path:
    """`--export-league-archive game:league` CLI escape hatch: export one
    specific league now regardless of whether it's the currently-active
    one -- e.g. backfilling an already-ended league (spec item 21's
    "Mirage that already ended" example) retroactively.

    Streaming and read-only on the database (`archive_runner`): the live DB and
    every `extra_sources` copy are opened `mode=ro`, nothing is held in RAM
    beyond one output leaf. `rebuild` ignores the existing archive files and
    keeps them as `*.legacy`. Returns the path of the base archive file."""
    publisher = None
    if publish:
        firebase_cfg = cfg["firebase"]
        publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))
    resume_hours = float(cfg.get("skill_meta", {}).get("collect", {}).get("resume_max_age_hours", 48))
    result = export_league_archive_streaming(
        Path(db_path), game, league, archive_dir=archive_dir, publisher=publisher, extra_sources=extra_sources,
        rebuild=rebuild, resume_max_age_hours=resume_hours,
    )
    return result.base_path


def run_cleanup_once(cfg: dict[str, Any]) -> None:
    """Daily cleanup (spec 9): char_items > 90d, char_details > 30d,
    skill-centric result rows (skill_stat_counts/skill_passive_counts/
    skill_populations) > 45d (safe: archive_export.py's freeze/merge guard
    keeps the per-league archive from shrinking once these age out -- see
    that module's docstring and config.yaml's retention comment),
    meta/*.json on Firebase > 30d (never "latest.json"),
    data/failed_responses/* > 30d, and (phase 7) event-league price files for
    leagues that are no longer active. Each step is independent and
    best-effort — one failing doesn't block the others."""
    retention_cfg = cfg.get("retention", {})

    db = LocalDb()
    try:
        deleted_rows = db.cleanup_old_char_items(older_than_days=retention_cfg.get("char_items_days", 90))
        deleted_details = db.cleanup_old_char_details(older_than_days=retention_cfg.get("char_details_days", 30))
        deleted_skill_stats = db.cleanup_old_skill_stat_counts(
            older_than_days=retention_cfg.get("skill_stat_counts_days", 45)
        )
        deleted_checkpoints = db.cleanup_finished_skill_run_checkpoints()
        log.info("cleanup: removed %d stale char_details row(s)", deleted_details)
        log.info("cleanup job: deleted %d old char_items rows", deleted_rows)
        log.info("cleanup job: deleted old skill-centric result rows: %s", deleted_skill_stats)
        log.info("cleanup job: deleted %d finished-run checkpoint row(s) (safety net)", deleted_checkpoints)
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


def _safe_run(fn, cfg: dict[str, Any], job_name: str, **kwargs: Any) -> None:
    try:
        fn(cfg, **kwargs)
    except Exception:
        log.error("%s job crashed", job_name, exc_info=True)


DEFAULT_AUTO_END_DATE_OFFSET_DAYS = 21


def _resolve_league_window(
    game_league_cfg: Any, http: HttpClient, game: str, auto_offset_days: int, today: date,
) -> tuple[Optional[date], Optional[date], str]:
    """Resolves one game's league-start window from its
    `schedule.league_start_mode.{game}` config value. Returns
    `(start_date, end_date, source)` where `source` is one of:

    - `"manual"` -- `game_league_cfg` is a `{start_date, end_date}` dict and
      both parsed cleanly with `start_date <= end_date`. Manual always wins
      over auto; this is never overridden.
    - `"manual-malformed"` -- was a dict, but a date failed to parse or
      `start_date` is after `end_date`. Logged at ERROR (a config typo
      should be noticed), falls through to auto for this game rather than
      crashing the scheduler.
    - `"auto"` -- `game_league_cfg` is the string `"auto"`, absent, or
      manual parsing failed, and `LeagueDatesFetcher(game).find_upcoming_launch()`
      found a real announcement whose derived window (launch date +
      `auto_offset_days`) hasn't fully lapsed yet.
    - `"auto-stale"` -- same as `"auto"`, but Steam news still had an old
      announcement whose entire derived window (including the offset
      buffer) is already in the past -- e.g. the currently most-recent
      "launch on <date>" post in the feed is from a league that already
      launched and finished its league_start_mode window months ago, and
      nothing newer has been posted since. Functionally identical to
      `"auto-not-found"` (`in_range` is always `False`), kept as a
      distinct label purely so a human reading `status.json` doesn't
      mistake stale historical detection for a fresh one.
    - `"auto-not-found"` -- auto mode, no "launch on <date>"-shaped
      announcement anywhere in Steam news at all right now (the normal
      state for most of a league's life) -- `start_date`/`end_date` are
      both `None`."""
    if isinstance(game_league_cfg, dict):
        try:
            start = date.fromisoformat(game_league_cfg["start_date"])
            end = date.fromisoformat(game_league_cfg["end_date"])
            if start > end:
                raise ValueError(f"start_date {start} is after end_date {end}")
        except (KeyError, ValueError, TypeError) as e:
            log.error(
                "scheduler: malformed schedule.league_start_mode.%s (%s) -- falling back to auto-detection",
                game, e,
            )
        else:
            return start, end, "manual"

    try:
        detected = LeagueDatesFetcher(http, game=game).find_upcoming_launch()
    except Exception:
        log.warning("scheduler: auto-detection failed for %s league_start_mode", game, exc_info=True)
        detected = None
    if detected is None:
        return None, None, "auto-not-found"

    start = detected.date()
    end = start + timedelta(days=auto_offset_days)
    if end < today:
        return start, end, "auto-stale"
    log.info("scheduler: auto-detected upcoming %s launch on %s -- league_start_mode window %s..%s", game, start, start, end)
    return start, end, "auto"


def _compute_meta_trigger(
    schedule_cfg: dict[str, Any], cfg: dict[str, Any], http: HttpClient, today: Optional[date] = None,
) -> tuple[BaseTrigger, bool, dict[str, dict[str, Any]]]:
    """Returns (trigger, is_league_start_active, per_game_detail) for the
    meta job's cadence right now. `today` defaults to the current
    Asia/Bangkok calendar date when not passed -- an explicit parameter
    purely so tests can pin it without mocking `datetime.now` globally
    (same pattern `_check_stats_cache_stale` already uses instead of a
    time-mocking dependency).

    `schedule.league_start_mode` (spec 9) resolves a window **per game**
    (`cfg["game_versions"]`, not a hardcoded poe1/poe2 pair -- a future
    third game gets this for free) via `_resolve_league_window`, each
    either manually configured or auto-detected from
    `LeagueDatesFetcher.find_upcoming_launch()`. league_start_mode is
    active -- overriding the manual `meta_runs_per_day_league_start` knob
    -- if **any** game's window contains today (both inclusive, explicit
    Asia/Bangkok calendar dates -- see the `momentTimezone` lesson in
    `src/fetchers/news.py`: never assume a bare date means what the
    reader's own locale assumes; the same union-across-games precedent the
    manual knob already established, since one meta invocation covers both
    games). `per_game_detail` (for `status.json`) is
    `{game: {"source", "start_date", "end_date", "in_range"}}` --
    `source` is one of `"manual"` / `"manual-malformed"` / `"auto"` /
    `"auto-stale"` / `"auto-not-found"`, see `_resolve_league_window`.

    Falls back to the existing manual-knob cron-or-interval logic
    unchanged when nothing is active -- that knob stays available as an
    escape hatch for an unplanned/delayed league."""
    if today is None:
        today = datetime.now(ZoneInfo("Asia/Bangkok")).date()

    league_start_root = schedule_cfg.get("league_start_mode") or {}
    auto_offset_days = league_start_root.get("auto_end_date_offset_days", DEFAULT_AUTO_END_DATE_OFFSET_DAYS)

    per_game_detail: dict[str, dict[str, Any]] = {}
    any_active = False
    for game in cfg.get("game_versions", ["poe2"]):
        game_league_cfg = league_start_root.get(game)
        start, end, source = _resolve_league_window(game_league_cfg, http, game, auto_offset_days, today)
        in_range = start is not None and end is not None and start <= today <= end
        any_active = any_active or in_range
        per_game_detail[game] = {
            "source": source,
            "start_date": start.isoformat() if start else None,
            "end_date": end.isoformat() if end else None,
            "in_range": in_range,
        }

    if any_active:
        runs_per_day = league_start_root.get("meta_runs_per_day", 4)
        return IntervalTrigger(hours=24 / runs_per_day), True, per_game_detail

    meta_runs_per_day = schedule_cfg.get("meta_runs_per_day_league_start", 1)
    meta_hour = schedule_cfg.get("meta_cron_hour_thailand", 2)
    if meta_runs_per_day <= 1:
        return CronTrigger(hour=meta_hour, minute=0, timezone="Asia/Bangkok"), False, per_game_detail
    return IntervalTrigger(hours=24 / meta_runs_per_day), False, per_game_detail


# Tracks whether league_start_mode is currently active, so
# _check_league_start_mode only reschedules the meta job (and rewrites
# status.json) on an actual state change, not every hourly check. Same
# module-level-singleton style as _shared_rate_limiter.
_league_start_mode_active: Optional[bool] = None


def _update_league_start_mode_status(
    publisher: PricesPublisher, active: bool, per_game_detail: dict[str, dict[str, Any]],
) -> None:
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    status["league_start_mode"] = "active" if active else "inactive"
    status["league_start_mode_detail"] = per_game_detail
    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def _check_league_start_mode(
    scheduler: BlockingScheduler, cfg: dict[str, Any], http: HttpClient, publisher: PricesPublisher,
    today: Optional[date] = None,
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
    same test-only purpose. `status.json`'s `league_start_mode_detail` is
    refreshed on every state change (not every hourly tick) -- good enough
    for the "notice if it's active when it shouldn't be" safety net a
    wrong auto-detection needs (spec 9): a bad detection can only ever cost
    extra meta-job frequency in the wrong window, never wrong data, and
    that mistake is visible in status.json the moment it starts."""
    global _league_start_mode_active
    trigger, active, per_game_detail = _compute_meta_trigger(cfg.get("schedule", {}), cfg, http, today=today)

    if active != _league_start_mode_active:
        scheduler.reschedule_job("meta", trigger=trigger)
        log.info(
            "scheduler: league_start_mode %s -> %s, meta job rescheduled to %s (%s)",
            "active" if _league_start_mode_active else "inactive",
            "active" if active else "inactive",
            trigger, per_game_detail,
        )
        _league_start_mode_active = active
        _update_league_start_mode_status(publisher, active, per_game_detail)


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
    http = _build_http_client(cfg)

    prices_interval = schedule_cfg.get("prices_interval_minutes", 180)
    scheduler.add_job(
        # next_run_time forces one run right at startup instead of waiting a
        # full interval -- matters now that prices_interval is a full day
        # (2026-09-10): without it, a deploy/restart leaves prices unpublished
        # for ~24h and trips both monitoring layers on a healthy process.
        _safe_run, IntervalTrigger(minutes=prices_interval), args=[run_prices_once, cfg, "prices"],
        id="prices", max_instances=1, misfire_grace_time=300,
        next_run_time=datetime.now(timezone.utc),
    )

    global _league_start_mode_active
    meta_trigger, _league_start_mode_active, per_game_detail = _compute_meta_trigger(schedule_cfg, cfg, http)
    scheduler.add_job(
        # parts=("slot",) explicitly: the skill-centric part has its own
        # dedicated job/cron/ping below (approved 2026-09-22) so it never
        # doubles up with this one, regardless of skill_meta.collect.scheduled.
        _safe_run, meta_trigger, args=[run_meta_once, cfg, "meta"], kwargs={"parts": ("slot",)},
        id="meta", max_instances=1, misfire_grace_time=3600,
    )
    _update_league_start_mode_status(publisher, _league_start_mode_active, per_game_detail)

    # Skill-centric collection (approved 2026-09-22, after two full runs +
    # one measured incremental cycle per game -- see SPEC.md 8.10): its own
    # daily cron, deliberately separate from "meta" above so its schedule,
    # game order, and healthcheck are independent. Only registered when
    # skill_meta.collect.scheduled is true (see CollectConfig/config.yaml) --
    # left unregistered entirely otherwise, not just no-op'd at runtime, so
    # "is it scheduled at all" is answerable from the job list alone.
    # games=["poe1","poe2"] is a hard requirement (not game_versions' own
    # order, currently poe2-first): PoE1 must finish before PoE2 starts --
    # sequential, never parallel, since both peaked over 200MB RSS in the
    # first live runs and this VM is an e2-micro. Placed at 05:00 ICT: after
    # cleanup (04:00) and gems (03:00), with the whole ~21h gap back to the
    # next 02:00 slot-only meta run as headroom even for a much slower cycle
    # than the ~71min measured 2026-09-22 (e.g. a league reset, where every
    # character is new and cache reuse collapses).
    skill_meta_scheduled = cfg.get("skill_meta", {}).get("collect", {}).get("scheduled", False)
    if skill_meta_scheduled:
        skill_meta_hour = schedule_cfg.get("skill_meta_cron_hour_thailand", 5)
        scheduler.add_job(
            _safe_run, CronTrigger(hour=skill_meta_hour, minute=0, timezone="Asia/Bangkok"),
            args=[run_meta_once, cfg, "skill_meta"], kwargs={"games": ["poe1", "poe2"], "parts": ("skill",)},
            id="skill_meta", max_instances=1, misfire_grace_time=3600,
        )

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

    # Cheap Firebase-to-Firebase copy (no external API calls) -- daily is
    # plenty since the source (refresh-stats.yml) only refreshes weekly.
    # Between meta (02:00) and cleanup (04:00 default) so a same-day gems
    # refresh lands before cleanup's retention sweep, not that it matters
    # much here (gems.json is a "latest.json"-style file, never aged out).
    gems_hour = schedule_cfg.get("gems_cron_hour_thailand", 3)
    scheduler.add_job(
        _safe_run, CronTrigger(hour=gems_hour, minute=0, timezone="Asia/Bangkok"),
        args=[run_gems_once, cfg, "gems"], id="gems", max_instances=1, misfire_grace_time=3600,
        next_run_time=datetime.now(timezone.utc),
    )

    # Public alias contract: same cheap staged->public copy, 10 minutes after gems.
    scheduler.add_job(
        _safe_run, CronTrigger(hour=gems_hour, minute=10, timezone="Asia/Bangkok"),
        args=[run_aliases_once, cfg, "aliases"], id="aliases", max_instances=1, misfire_grace_time=3600,
        next_run_time=datetime.now(timezone.utc),
    )

    # No healthcheck ping for this one -- it's scheduler bookkeeping, not a
    # data-fetching job; heartbeat.yml's existing META_MAX_AGE_HOURS=26
    # tolerance already covers the meta job itself at any active frequency.
    scheduler.add_job(
        _check_league_start_mode, IntervalTrigger(hours=1), args=[scheduler, cfg, http, publisher],
        id="league_start_mode_check", max_instances=1, misfire_grace_time=1800,
    )

    # Weekly safety-net re-export of whatever league is currently active
    # (skill-meta phase 5, item 21) -- a league *change* already triggers
    # its own archive export from inside _run_meta_for_game, this just
    # bounds how much of an in-progress league could be lost if the VM
    # disappears mid-league to at most a week. No poe.ninja calls (reads
    # local SQLite only), so no healthcheck ping needed here either.
    # A fixed weekly cron, not IntervalTrigger(weeks=1): an interval's first
    # fire is "process start + 7 days", so every restart moved it and the
    # 2026-10-15 OOM landed exactly 7 days after a restart, at a time nobody
    # chose. Registered even while `archive.enabled` is false so the skip is
    # logged (WARNING) every cycle rather than the job just vanishing.
    archive_dow = schedule_cfg.get("archive_day_of_week", "sun")
    archive_hour = schedule_cfg.get("archive_hour_thailand", 13)
    scheduler.add_job(
        _safe_run, CronTrigger(day_of_week=archive_dow, hour=archive_hour, minute=0, timezone="Asia/Bangkok"),
        args=[run_archive_export_once, cfg, "archive"],
        id="archive", max_instances=1, misfire_grace_time=3600,
    )
    if not _archive_enabled(cfg):
        log.warning("scheduler: archive export is DISABLED (%s) -- no league archive will be written", _archive_disabled_reason(cfg))
    _warn_unarchived_leagues(_publish_archive_status(publisher, cfg))
    _clear_stale_running_jobs(publisher)

    log.info(
        "scheduler: starting (prices every %dmin, meta %s [league_start_mode %s: %s], cleanup daily %02d:00 ICT, "
        "news every %dmin, gems daily %02d:00 ICT, archive %s %02d:00 ICT%s, skill_meta %s)",
        prices_interval, meta_trigger, "active" if _league_start_mode_active else "inactive", per_game_detail,
        cleanup_hour, news_interval, gems_hour, archive_dow, archive_hour, "" if _archive_enabled(cfg) else " [DISABLED]",
        f"daily {schedule_cfg.get('skill_meta_cron_hour_thailand', 5):02d}:00 ICT (poe1 then poe2)" if skill_meta_scheduled else "not scheduled",
    )
    scheduler.start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-prices-once", action="store_true")
    parser.add_argument("--run-meta-once", action="store_true")
    parser.add_argument(
        "--game", action="append", choices=["poe1", "poe2"],
        help="With --run-meta-once: restrict to this game (repeatable). Default: every configured game.",
    )
    parser.add_argument(
        "--only", choices=list(META_PARTS),
        help="With --run-meta-once: 'slot' = legacy per-slot job only, 'skill' = skill-centric job only. Default: both.",
    )
    parser.add_argument("--run-cleanup-once", action="store_true")
    parser.add_argument("--run-news-once", action="store_true")
    parser.add_argument("--run-gems-once", action="store_true")
    parser.add_argument("--run-aliases-once", action="store_true", help="Copy the staged public alias contract to {game}/aliases/latest.json")
    parser.add_argument("--reprocess-char-items", action="store_true", help="Recompute skill_mod_counts from stored char_items, offline (skill-meta phase 2)")
    parser.add_argument("--export-league-archive", metavar="GAME:LEAGUE", help="Export one league's archive now, e.g. poe1:Mirage (skill-meta phase 5)")
    parser.add_argument(
        "--extra-source", action="append", default=[], metavar="SQLITE",
        help="With --export-league-archive: an insurance copy of the database to also merge snapshots from "
             "(repeatable; opened read-only; must carry snapshot_id indexes -- tools/prepare_archive_copy.py)",
    )
    parser.add_argument(
        "--rebuild-archive", action="store_true",
        help="With --export-league-archive: ignore the existing archive files (kept as *.legacy) and build from the sources",
    )
    parser.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE_DIR), help="With --export-league-archive: output directory")
    parser.add_argument("--archive-db", default=str(DEFAULT_DB_PATH), help="With --export-league-archive: database to read (opened read-only)")
    parser.add_argument("--no-publish", action="store_true", help="With --export-league-archive: write the files only, skip Firebase")
    parser.add_argument(
        "--publish-skill-meta", action="store_true",
        help="(Re)publish {game}/skills/* + passives/latest.json from the latest finished skill-centric run, "
             "without recollecting (needs --game at least once)",
    )
    parser.add_argument("--serve", action="store_true", help="Run the persistent scheduler (prices+meta+cleanup+news+gems)")
    args = parser.parse_args()

    cfg = load_config()
    _setup_logging(cfg)

    if args.run_prices_once:
        run_prices_once(cfg)
    elif args.run_meta_once:
        run_meta_once(cfg, games=args.game, parts=(args.only,) if args.only else None)
    elif args.run_cleanup_once:
        run_cleanup_once(cfg)
    elif args.run_news_once:
        run_news_once(cfg)
    elif args.run_gems_once:
        run_gems_once(cfg)
    elif args.run_aliases_once:
        run_aliases_once(cfg)
    elif args.reprocess_char_items:
        run_reprocess_char_items(cfg)
    elif args.publish_skill_meta:
        if not args.game:
            parser.error("--publish-skill-meta requires at least one --game")
        run_publish_skill_meta_once(cfg, args.game)
    elif args.export_league_archive:
        game, _, league = args.export_league_archive.partition(":")
        if not game or not league:
            parser.error("--export-league-archive expects GAME:LEAGUE, e.g. poe1:Mirage")
        run_export_league_archive(
            cfg, game, league, archive_dir=Path(args.archive_dir), db_path=Path(args.archive_db),
            extra_sources=[Path(p) for p in args.extra_source], rebuild=args.rebuild_archive, publish=not args.no_publish,
        )
    elif args.serve:
        run_scheduler(cfg)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
