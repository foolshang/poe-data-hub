"""Entry point.

    python -m src.main --run-prices-once
    python -m src.main --run-meta-once
    python -m src.main --run-cleanup-once
    python -m src.main --serve      # persistent scheduler (systemd uses this)
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import logging.handlers
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.fetchers import ninja_prices, scout_prices
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
from src.storage.publisher import PricesPublisher, build_meta_schema, build_prices_schema
from src.utils.config import load_config
from src.utils.http_client import HttpClient
from src.utils.rate_limiter import RateLimiter

log = logging.getLogger("poe_data_hub")

PRICES_BLOB_PATH = "poe2/prices/latest.json"
META_BLOB_PATH_LATEST = "poe2/meta/latest.json"
STATUS_BLOB_PATH = "status.json"


def _build_http_client(cfg: dict[str, Any]) -> HttpClient:
    rate_limiter = RateLimiter(default_interval_seconds=2.0, per_host=cfg.get("rate_limits", {}))
    http_cfg = cfg.get("http", {})
    return HttpClient(
        rate_limiter,
        max_retries=http_cfg.get("max_retries", 3),
        timeout_seconds=http_cfg.get("timeout_seconds", 15),
    )


def run_prices_once(cfg: dict[str, Any]) -> dict[str, Any]:
    http = _build_http_client(cfg)

    league_cfg = cfg.get("league", {})
    detector = LeagueDetector(
        http,
        game="poe2",
        league_override=league_cfg.get("league_override", ""),
        cache_ttl_seconds=league_cfg.get("cache_ttl_seconds", 12 * 60 * 60),
    )
    league_result = detector.detect()
    league = league_result.main
    log.info("prices job: detected league=%s events=%s", league, league_result.events)

    ninja_entries: list[dict[str, Any]] = []
    ninja_ok = True
    try:
        ninja_entries, ninja_counts = NinjaPricesFetcher(http).fetch_all(league)
        log.info("prices job: ninja fetched %d entries across %d categories", len(ninja_entries), len(ninja_counts))
    except Exception as e:
        ninja_ok = False
        log.error("prices job: ninja fetch failed entirely: %s", e, exc_info=True)

    scout_entries: list[dict[str, Any]] = []
    scout_ok = True
    try:
        scout_entries, scout_counts = ScoutPricesFetcher(http).fetch_all(league)
        log.info("prices job: scout fetched %d entries across %d categories", len(scout_entries), len(scout_counts))
    except Exception as e:
        scout_ok = False
        log.error("prices job: scout fetch failed entirely: %s", e, exc_info=True)

    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

    previous = publisher.fetch_previous(PRICES_BLOB_PATH)
    data = build_prices_schema(
        league=league,
        ninja_entries=ninja_entries,
        ninja_ok=ninja_ok,
        ninja_bucket_of=ninja_prices.category_bucket,
        scout_entries=scout_entries,
        scout_ok=scout_ok,
        scout_bucket_of=scout_prices.category_bucket,
        previous=previous,
    )

    publisher.publish(data, PRICES_BLOB_PATH, cache_control="public, max-age=300")
    log.info("prices job: published %d currency + %d item entries", len(data["currency"]), len(data["items"]))

    _update_status(publisher, ok=(ninja_ok and scout_ok), data=data)
    return data


def run_meta_once(cfg: dict[str, Any]) -> dict[str, Any]:
    job_start = time.monotonic()
    http = _build_http_client(cfg)

    league_cfg = cfg.get("league", {})
    detector = LeagueDetector(
        http,
        game="poe2",
        league_override=league_cfg.get("league_override", ""),
        cache_ttl_seconds=league_cfg.get("cache_ttl_seconds", 12 * 60 * 60),
    )
    league_result = detector.detect()
    league = league_result.main
    log.info("meta job: detected league=%s events=%s", league, league_result.events)

    league_dates = LeagueDatesFetcher(http).fetch(league)
    days_since_league_start: Optional[int] = (
        (datetime.now(timezone.utc) - league_dates.start_at).days if league_dates.start_at else None
    )
    log.info("meta job: league_dates start_at=%s end_at=%s", league_dates.start_at, league_dates.end_at)

    builds_fetcher = NinjaBuildsFetcher(http)
    snapshot = builds_fetcher.resolve_snapshot(league)

    by_slot, per_slot_counts = builds_fetcher.fetch_all(snapshot)
    log.info("meta job: fetched %d slot lists: %s", len(by_slot), per_slot_counts)

    normalizer = ModNormalizer(
        http, cache_ttl_seconds=cfg.get("ggg", {}).get("stats_cache_ttl_seconds", 7 * 24 * 60 * 60)
    )
    normalizer.load()
    archetyper = Archetyper()

    chars_fetcher = NinjaCharsFetcher(http)
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

    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))

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

    dated_path = f"poe2/meta/{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    publisher.publish(data, META_BLOB_PATH_LATEST, cache_control="public, max-age=3600")
    publisher.publish(data, dated_path, cache_control="public, max-age=3600")
    log.info("meta job: published %d mods (%d before count>=5 filter)", len(data["mods"]), len(all_mod_counts))

    unmapped_pct = (
        round(normalizer.unmapped_count / normalizer.processed_count * 100, 2)
        if normalizer.processed_count else 0.0
    )
    _update_meta_status(
        publisher,
        data=data,
        end_at=league_dates.end_at,
        summary={
            "duration_seconds": round(time.monotonic() - job_start, 1),
            "sample_total": sum(per_slot_counts.values()),
            "unique_characters_fetched": cache.fetched_count,
            "dedupe_savings": sum(per_slot_counts.values()) - cache.fetched_count,
            "characters_failed": cache.failed_count,
            "mods_processed": normalizer.processed_count,
            "unmapped_mod_pct": unmapped_pct,
        },
    )

    return data


def _update_status(publisher: PricesPublisher, ok: bool, data: dict[str, Any]) -> None:
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    status["last_prices"] = data["generated_at"]
    status["league"] = data["league"]
    status["sources"] = data["sources"]
    if not ok:
        status.setdefault("errors", []).append(
            {"ts": data["generated_at"], "job": "prices", "detail": data["sources"]}
        )
        status["errors"] = status["errors"][-10:]
    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def _update_meta_status(
    publisher: PricesPublisher,
    data: dict[str, Any],
    end_at: Optional[datetime],
    summary: dict[str, Any],
) -> None:
    status = publisher.fetch_previous(STATUS_BLOB_PATH) or {"errors": []}
    status["last_meta"] = data["generated_at"]
    status["league"] = data["league"]
    status["league_end_at"] = end_at.strftime("%Y-%m-%dT%H:%M:%SZ") if end_at else None
    status["meta_job_summary"] = summary
    if summary["unmapped_mod_pct"] >= 5.0:
        status.setdefault("errors", []).append(
            {
                "ts": data["generated_at"], "job": "meta",
                "detail": f"unmapped_mod_pct={summary['unmapped_mod_pct']} >= 5% threshold",
            }
        )
        status["errors"] = status["errors"][-10:]
    publisher.publish(status, STATUS_BLOB_PATH, cache_control="no-cache")


def run_cleanup_once(cfg: dict[str, Any]) -> None:
    """Daily cleanup (spec 9): char_items > 90d, meta/*.json on Firebase
    > 30d (never "latest.json"), data/failed_responses/* > 30d. Each step is
    independent and best-effort — one failing doesn't block the others."""
    retention_cfg = cfg.get("retention", {})

    db = LocalDb()
    try:
        deleted_rows = db.cleanup_old_char_items(older_than_days=retention_cfg.get("char_items_days", 90))
        log.info("cleanup job: deleted %d old char_items rows", deleted_rows)
    finally:
        db.close()

    firebase_cfg = cfg["firebase"]
    publisher = PricesPublisher(bucket_name=firebase_cfg["bucket"], project_id=firebase_cfg.get("project_id"))
    deleted_meta = publisher.delete_old_blobs(
        "poe2/meta/", older_than_days=retention_cfg.get("meta_files_days", 30), keep_names=frozenset({"latest.json"})
    )
    log.info("cleanup job: deleted %d old meta files from Firebase", deleted_meta)

    deleted_failed = _cleanup_old_local_files(
        Path("data/failed_responses"), older_than_days=retention_cfg.get("failed_responses_days", 30)
    )
    log.info("cleanup job: deleted %d old failed_response files", deleted_failed)


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


def run_scheduler(cfg: dict[str, Any]) -> None:
    """Persistent scheduler process (spec 9): prices every N minutes, meta on
    a Thailand-time cron (or several evenly-spaced runs/day if
    `meta_runs_per_day_league_start` > 1), cleanup daily. Jobs run
    independently -- a slow meta run never blocks the prices job."""
    schedule_cfg = cfg.get("schedule", {})
    scheduler = BlockingScheduler(timezone="UTC")

    prices_interval = schedule_cfg.get("prices_interval_minutes", 20)
    scheduler.add_job(
        _safe_run, IntervalTrigger(minutes=prices_interval), args=[run_prices_once, cfg, "prices"],
        id="prices", max_instances=1, misfire_grace_time=300,
    )

    meta_runs_per_day = schedule_cfg.get("meta_runs_per_day_league_start", 1)
    meta_hour = schedule_cfg.get("meta_cron_hour_thailand", 2)
    if meta_runs_per_day <= 1:
        meta_trigger = CronTrigger(hour=meta_hour, minute=0, timezone="Asia/Bangkok")
    else:
        meta_trigger = IntervalTrigger(hours=24 / meta_runs_per_day)
    scheduler.add_job(
        _safe_run, meta_trigger, args=[run_meta_once, cfg, "meta"],
        id="meta", max_instances=1, misfire_grace_time=3600,
    )

    cleanup_hour = schedule_cfg.get("cleanup_hour_thailand", 4)
    scheduler.add_job(
        _safe_run, CronTrigger(hour=cleanup_hour, minute=0, timezone="Asia/Bangkok"),
        args=[run_cleanup_once, cfg, "cleanup"], id="cleanup", max_instances=1, misfire_grace_time=3600,
    )

    log.info(
        "scheduler: starting (prices every %dmin, meta %s, cleanup daily %02d:00 ICT)",
        prices_interval, meta_trigger, cleanup_hour,
    )
    scheduler.start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-prices-once", action="store_true")
    parser.add_argument("--run-meta-once", action="store_true")
    parser.add_argument("--run-cleanup-once", action="store_true")
    parser.add_argument("--serve", action="store_true", help="Run the persistent scheduler (prices+meta+cleanup)")
    args = parser.parse_args()

    cfg = load_config()
    _setup_logging(cfg)

    if args.run_prices_once:
        run_prices_once(cfg)
    elif args.run_meta_once:
        run_meta_once(cfg)
    elif args.run_cleanup_once:
        run_cleanup_once(cfg)
    elif args.serve:
        run_scheduler(cfg)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
