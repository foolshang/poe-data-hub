"""Layer-2 monitoring (external, independent of Layer 1): fetches each
game's prices/latest.json and status.json from the *public* Firebase URLs
(no credentials needed) and fails (non-zero exit) if any looks stale, or if
more than half of any single source's categories are failing
(`sample.per_category`, added phase 7.1) -- a whole-source `sources.ninja:
"ok"` can hide a partial degradation, since NinjaPricesFetcher.fetch_all's
own per-category skip logic swallows individual category failures and
still reports the source as a whole as "ok" (see SPEC.md 8.1). `sample` is
an additive field (no schema_version bump) -- absent entirely on payloads
published before this check shipped, handled via `.get(..., {})` so this
never crashes on an old/missing field, it just skips the check.

Only each game's main `prices/latest.json` is checked here, not
`{game}/prices/events/*.json` -- event files legitimately don't exist most
of the time (no event league active), so their *absence* is not a problem;
`{game}/prices/index.json` is what tells a consumer whether any exist right
now (see SPEC.md 8.1b). A consumer trusting index.json already gets a
correct answer without this script needing to probe event files at all.

Also checks `{game}/news/latest.json` freshness for both games (phase 8;
approved cadence: news job runs every 5 minutes -- see SPEC.md). Does NOT
check `mtx/latest.json` (not requested/needed): the news job publishes both
files in the same run, so news freshness for either game is already a
reliable proxy for "did the news job run" as a whole.

Also checks each game's Hardcore-variant freshness (phase 9) -- but via
`{game}/prices/index.json`'s `hardcore` key, not by probing
`{game}/prices/hardcore/latest.json` directly: `hardcore` is legitimately
`null` (and the file legitimately doesn't exist) during a league-transition
window when no HC variant can be found, same reasoning as event files.
Only checks freshness when `index.hardcore` is present; a null value is
never flagged as a problem.

Meta freshness (phase 10: both games) reads `status.json`'s
`meta.{game}.last_meta` -- the nested per-game shape `_update_meta_status`
writes. Both games' meta run together in one job invocation (`run_meta_once`
loops poe2 then poe1 and writes both games' status in the same call), so
there's no asymmetric-migration window here the way prices/news had: the
first meta run under the new code populates both games' `status.meta.*` in
one write, not two separate job schedules drifting in and out of sync.

Also checks `meta.{game}.meta_job_summary.sample_total` is nonzero (added
2026-09-17, after the ninja_builds schema-break postmortem): freshness alone
missed that incident entirely, since the job kept publishing "successfully"
with a fresh timestamp every cycle for ~43 days while the actual character
count was silently 0. This check reads the population count directly rather
than trusting a fresh timestamp to imply real content.

Skill-centric collection (added 2026-09-22, once `skill_meta.collect.scheduled`
was approved): checks `{game}/skills/index.json`'s own `generated_at`
freshness and `population_total` nonzero, for both games -- the same
freshness-plus-content pairing as the meta checks above, same reasoning
(a fresh timestamp with silently-empty content already happened once). This
is a completely separate file from `status.json`'s `meta.*` -- the
skill-centric job has its own daily cron and its own Layer-1 healthchecks.io
check (`skill_meta_ping_url`), independent of the slot job's `meta_ping_url`.

This is deliberately a second, independent system from the
healthchecks.io dead-man-switch pings the hub sends itself (Layer 1, see
src/main.py's _ping_healthcheck and CLAUDE.md): Layer 1 watches "is the
process alive and did publish succeed", from the inside, via
healthchecks.io. Layer 2 watches "is the actual published data fresh", from
the outside, via GitHub Actions -- a different provider, a different
failure mode covered (e.g. the hub pings successfully but somehow keeps
publishing stale data, or healthchecks.io itself has an outage).

Usage:
    python -m tools.heartbeat_check

Meant to run from .github/workflows/heartbeat.yml on an hourly cron.
Needs no secrets: everything it reads is already public.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Optional

import requests

PRICES_URLS = {
    "poe2": "https://storage.googleapis.com/poe-data-hub/poe2/prices/latest.json",
    "poe1": "https://storage.googleapis.com/poe-data-hub/poe1/prices/latest.json",
}
NEWS_URLS = {
    "poe2": "https://storage.googleapis.com/poe-data-hub/poe2/news/latest.json",
    "poe1": "https://storage.googleapis.com/poe-data-hub/poe1/news/latest.json",
}
INDEX_URLS = {
    "poe2": "https://storage.googleapis.com/poe-data-hub/poe2/prices/index.json",
    "poe1": "https://storage.googleapis.com/poe-data-hub/poe1/prices/index.json",
}
SKILLS_INDEX_URLS = {
    "poe2": "https://storage.googleapis.com/poe-data-hub/poe2/skills/index.json",
    "poe1": "https://storage.googleapis.com/poe-data-hub/poe1/skills/index.json",
}
STATUS_URL = "https://storage.googleapis.com/poe-data-hub/status.json"
# Everything the weekly refresh-stats.yml produces (the GGG stats table, the gems table, the rendering aliases)
STATS_CACHE_URLS = {
    "poe2": "https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    "poe1": "https://storage.googleapis.com/poe-data-hub/internal/stats_cache_poe1.json",
}
GEMS_URLS = {
    "poe2": "https://storage.googleapis.com/poe-data-hub/poe2/gems.json",
    "poe1": "https://storage.googleapis.com/poe-data-hub/poe1/gems.json",
}
ALIASES_URLS = {
    "poe2": "https://storage.googleapis.com/poe-data-hub/poe2/aliases/latest.json",
    "poe1": "https://storage.googleapis.com/poe-data-hub/poe1/aliases/latest.json",
}

# prices publish every 1440 minutes (config.yaml schedule.prices_interval_minutes) x 2 as a
# retry buffer -- not read from config.yaml (this script is deliberately independent of the
# rest of the repo, see module docstring), so bump this by hand if that interval changes.
PRICES_MAX_AGE_MINUTES = 2880
META_MAX_AGE_HOURS = 26
# skill-centric job runs once/day (schedule.skill_meta_cron_hour_thailand) --
# same by-hand-bump caveat as the other constants here. Slightly wider than
# META_MAX_AGE_HOURS: it's a much heavier job (measured ~71min combined for
# both games 2026-09-22) with more room for a slow cycle, and Layer 2 should
# generally be at least as lenient as Layer 1's healthchecks.io Period+Grace
# (recommended 24h+4h=28h there) so the two layers don't flap in disagreement.
SKILLS_INDEX_MAX_AGE_HOURS = 30
# news job runs every 5 minutes (schedule.news_interval_minutes) -- same
# by-hand-bump caveat as above; kept at a ~3x buffer (vs. prices' 2x) since
# news freshness matters more.
NEWS_MAX_AGE_MINUTES = 15
# refresh-stats.yml is a WEEKLY GitHub cron, and GitHub's cron is late on this account: measured 2026-10-20, every
# weekly run since 2026-09-07 started 4.7-7 hours after its slot (never skipped so far). 7 days + 7 h is the normal
# worst case, so 9 days never false-alarms on lateness and a skipped/failing weekly run is flagged about two days
# after it was due (not "the same hour": this layer's own latency is hours, see CLAUDE.md).
WEEKLY_ARTIFACT_MAX_AGE_DAYS = 9
# "fresh is not the same as real": floors well under today's counts (stats cache PoE2 8,306 / PoE1 18,189 entries,
# gems PoE2 1,120 / PoE1 915, PoE1 aliases 1,967) so a silent schema change or an empty/truncated upload is caught.
MIN_STATS_ENTRIES = 3000
MIN_GEMS = 300
MIN_POE1_ALIASES = 500
# league archive (SPEC 8.13): red when a snapshot that is in neither the archive nor an insurance copy will be deleted by
# retention within this many days. Not a freshness guess: the date is the hub's own computation (created_at + 45 days, next
# 21:00 UTC cleanup) and this layer's latency is hours, so a week still leaves time to act.
ARCHIVE_LOSS_WARNING_DAYS = 7
# the weekly export (Sunday 13:00 ICT) plus the same GitHub lateness margin as WEEKLY_ARTIFACT_MAX_AGE_DAYS; only checked
# while the archive is enabled
ARCHIVE_EXPORT_MAX_AGE_DAYS = 9
KNOWN_GEMS_SCHEMA_VERSION = 1
KNOWN_ALIASES_SCHEMA_VERSION = 1


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_prices_freshness(generated_at: str, now: Optional[datetime] = None) -> Optional[str]:
    """Returns a problem description if stale, None if fresh."""
    now = now or datetime.now(timezone.utc)
    age_minutes = (now - _parse_iso(generated_at)).total_seconds() / 60
    if age_minutes > PRICES_MAX_AGE_MINUTES:
        return f"prices/latest.json generated_at is {age_minutes:.0f} minutes old (> {PRICES_MAX_AGE_MINUTES})"
    return None


def check_news_freshness(generated_at: str, now: Optional[datetime] = None) -> Optional[str]:
    """Returns a problem description if stale, None if fresh."""
    now = now or datetime.now(timezone.utc)
    age_minutes = (now - _parse_iso(generated_at)).total_seconds() / 60
    if age_minutes > NEWS_MAX_AGE_MINUTES:
        return f"news/latest.json generated_at is {age_minutes:.0f} minutes old (> {NEWS_MAX_AGE_MINUTES})"
    return None


def check_hardcore_freshness(generated_at: str, now: Optional[datetime] = None) -> Optional[str]:
    """Returns a problem description if stale, None if fresh. Only called
    when index.json's `hardcore` key is non-null -- a null value (no HC
    variant found this cycle) is never a problem, so callers must check
    that before calling this."""
    now = now or datetime.now(timezone.utc)
    age_minutes = (now - _parse_iso(generated_at)).total_seconds() / 60
    if age_minutes > PRICES_MAX_AGE_MINUTES:
        return f"prices/hardcore/latest.json generated_at is {age_minutes:.0f} minutes old (> {PRICES_MAX_AGE_MINUTES})"
    return None


def check_meta_freshness(last_meta: str, now: Optional[datetime] = None) -> Optional[str]:
    """Returns a problem description if stale, None if fresh."""
    now = now or datetime.now(timezone.utc)
    age_hours = (now - _parse_iso(last_meta)).total_seconds() / 3600
    if age_hours > META_MAX_AGE_HOURS:
        return f"meta.last_meta is {age_hours:.1f} hours old (> {META_MAX_AGE_HOURS})"
    return None


def check_meta_sample_total(meta_job_summary: Optional[dict]) -> Optional[str]:
    """Returns a problem description if `sample_total` is 0 or missing --
    freshness alone (check_meta_freshness) cannot catch this: the
    2026-08-04-to-09-16 ninja_builds schema break kept `last_meta` fresh on
    every cycle (the job published successfully every time) while the
    actual character sample was silently empty for ~43 days. This checks
    the population count directly instead of trusting a fresh timestamp to
    imply real content."""
    sample_total = (meta_job_summary or {}).get("sample_total")
    if not sample_total:
        return f"meta.meta_job_summary.sample_total is {sample_total!r} (expected > 0)"
    return None


def check_skills_index_freshness(generated_at: str, now: Optional[datetime] = None) -> Optional[str]:
    """Returns a problem description if stale, None if fresh."""
    now = now or datetime.now(timezone.utc)
    age_hours = (now - _parse_iso(generated_at)).total_seconds() / 3600
    if age_hours > SKILLS_INDEX_MAX_AGE_HOURS:
        return f"skills/index.json generated_at is {age_hours:.1f} hours old (> {SKILLS_INDEX_MAX_AGE_HOURS})"
    return None


def check_skills_index_population(population_total: Optional[int]) -> Optional[str]:
    """Same "content, not just freshness" reasoning as check_meta_sample_total."""
    if not population_total:
        return f"skills/index.json population_total is {population_total!r} (expected > 0)"
    return None


def _age_days_from_iso(ts: str, now: Optional[datetime]) -> float:
    now = now or datetime.now(timezone.utc)
    return (now - _parse_iso(ts)).total_seconds() / 86400


def check_stats_cache(game: str, envelope: object, now: Optional[datetime] = None) -> list[str]:
    """`internal/stats_cache*.json` (written weekly by refresh-stats.yml): age AND real content. Shape
    {"fetched_at": epoch_seconds, "data": {"result": [{"id", "entries": [...]}, ...]}}."""
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict) or not isinstance(envelope["data"].get("result"), list):
        return [f"{game}: stats cache has an unexpected shape ({type(envelope).__name__})"]
    fetched_at = envelope.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        problems.append(f"{game}: stats cache has no numeric fetched_at ({fetched_at!r})")
    else:
        age_days = (now.timestamp() - fetched_at) / 86400
        if age_days > WEEKLY_ARTIFACT_MAX_AGE_DAYS:
            problems.append(f"{game}: stats cache fetched_at is {age_days:.1f} days old (> {WEEKLY_ARTIFACT_MAX_AGE_DAYS})")
    total = sum(len(g.get("entries", [])) for g in envelope["data"]["result"] if isinstance(g, dict))
    if total < MIN_STATS_ENTRIES:
        problems.append(f"{game}: stats cache holds {total} entries (< {MIN_STATS_ENTRIES})")
    return problems


def check_gems(game: str, doc: object, now: Optional[datetime] = None) -> list[str]:
    """`{game}/gems.json` (staged weekly, copied daily): schema, age of its own generated_at, real content."""
    if not isinstance(doc, dict):
        return [f"{game}: gems.json is not an object"]
    problems: list[str] = []
    if doc.get("schema_version") != KNOWN_GEMS_SCHEMA_VERSION:
        problems.append(f"{game}: gems.json schema_version is {doc.get('schema_version')!r} (this check knows {KNOWN_GEMS_SCHEMA_VERSION})")
    try:
        age = _age_days_from_iso(doc["generated_at"], now)
        if age > WEEKLY_ARTIFACT_MAX_AGE_DAYS:
            problems.append(f"{game}: gems.json generated_at is {age:.1f} days old (> {WEEKLY_ARTIFACT_MAX_AGE_DAYS})")
    except (KeyError, ValueError, TypeError):
        problems.append(f"{game}: gems.json has no parseable generated_at")
    n = len(doc["gems"]) if isinstance(doc.get("gems"), list) else 0
    if n < MIN_GEMS:
        problems.append(f"{game}: gems.json holds {n} gems (< {MIN_GEMS})")
    return problems


def check_aliases(game: str, doc: object, now: Optional[datetime] = None) -> list[str]:
    """`{game}/aliases/latest.json` (public contract, SPEC.md 8.12): schema, game, age, real content. PoE2 may
    legitimately have few aliases (RePoE links only 292 of 10,759 entries), PoE1 must have many."""
    if not isinstance(doc, dict):
        return [f"{game}: aliases/latest.json is not an object"]
    problems: list[str] = []
    if doc.get("schema_version") != KNOWN_ALIASES_SCHEMA_VERSION:
        problems.append(f"{game}: aliases schema_version is {doc.get('schema_version')!r} (this check knows {KNOWN_ALIASES_SCHEMA_VERSION})")
    if doc.get("game") != game:
        problems.append(f"{game}: aliases file says game={doc.get('game')!r}")
    try:
        age = _age_days_from_iso(doc["generated_at"], now)
        if age > WEEKLY_ARTIFACT_MAX_AGE_DAYS:
            problems.append(f"{game}: aliases generated_at is {age:.1f} days old (> {WEEKLY_ARTIFACT_MAX_AGE_DAYS})")
    except (KeyError, ValueError, TypeError):
        problems.append(f"{game}: aliases has no parseable generated_at")
    if not isinstance(doc.get("aliases"), list) or not isinstance(doc.get("stat_groups"), list) or not isinstance(doc.get("stat_aliases"), dict):
        problems.append(f"{game}: aliases file is missing aliases / stat_groups / stat_aliases")
    elif game == "poe1" and len(doc["aliases"]) < MIN_POE1_ALIASES:
        problems.append(f"poe1: aliases holds {len(doc['aliases'])} entries (< {MIN_POE1_ALIASES})")
    return problems


def check_archive(archive: object, now: Optional[datetime] = None) -> list[str]:
    """`status.json` -> `archive` (written by the hub, see main._publish_archive_status).

    Always: a league with snapshots in NEITHER the archive nor an insurance copy whose first retention deletion is within
    ARCHIVE_LOSS_WARNING_DAYS. A switched-off archive is NOT a problem by itself (it is switched off on purpose, the hub
    reports `enabled: false`); only the loss it allows is.
    Only while `enabled`: the latest export of a game failed and the last good one is older than
    ARCHIVE_EXPORT_MAX_AGE_DAYS (or never happened), or a league's state file cannot be read. An absent block or one
    without `coverage` (a hub that does not write it yet) is not a problem."""
    if not isinstance(archive, dict):
        return []
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []
    coverage = archive.get("coverage") if isinstance(archive.get("coverage"), dict) else {}
    for entry in coverage.get("leagues") or []:
        loss_at = entry.get("earliest_loss_at")
        if not loss_at:
            continue
        days_left = (_parse_iso(loss_at) - now).total_seconds() / 86400
        if days_left <= ARCHIVE_LOSS_WARNING_DAYS:
            problems.append(
                f"archive: {entry.get('game')} {entry.get('league')!r}: {entry.get('at_risk_count')} snapshot(s) are in neither "
                f"the archive nor an insurance copy and retention deletes the first at {loss_at} ({days_left:.1f} days)"
            )
    if archive.get("enabled"):
        for entry in coverage.get("leagues") or []:
            if entry.get("archive_state_error"):
                problems.append(f"archive: {entry.get('game')} {entry.get('league')!r}: state file unreadable ({entry['archive_state_error']})")
        for game, last in (archive.get("last_export") or {}).items():
            if last.get("status") == "ok":
                continue
            last_ok = last.get("last_ok_at")
            age_days = (now - _parse_iso(last_ok)).total_seconds() / 86400 if last_ok else None
            if age_days is None or age_days > ARCHIVE_EXPORT_MAX_AGE_DAYS:
                problems.append(
                    f"archive: {game}: latest export {last.get('status')} ({last.get('error_class') or last.get('error')}) and "
                    + ("no export has ever succeeded" if age_days is None else f"the last good one is {age_days:.1f} days old")
                )
    return problems


def check_per_category_health(source: str, per_category: dict) -> Optional[str]:
    """Returns a problem description if more than half of `source`'s
    categories have "ok": False, None otherwise (including when
    per_category is empty -- nothing to flag)."""
    if not per_category:
        return None
    total = len(per_category)
    failed = sum(1 for v in per_category.values() if not v.get("ok", True))
    if failed > total / 2:
        return f"{source}: {failed}/{total} categories failing (> half)"
    return None


def run() -> int:
    problems: list[str] = []

    for game, url in PRICES_URLS.items():
        try:
            prices = requests.get(url, timeout=15).json()
            problem = check_prices_freshness(prices["generated_at"])
            if problem:
                problems.append(f"{game}: {problem}")

            per_category_by_source = (prices.get("sample") or {}).get("per_category") or {}
            for source, per_category in per_category_by_source.items():
                problem = check_per_category_health(source, per_category)
                if problem:
                    problems.append(f"{game}: {problem}")
        except Exception as e:
            problems.append(f"could not fetch/parse {url}: {e}")

    for game, url in NEWS_URLS.items():
        try:
            news = requests.get(url, timeout=15).json()
            problem = check_news_freshness(news["generated_at"])
            if problem:
                problems.append(f"{game}: {problem}")
        except Exception as e:
            problems.append(f"could not fetch/parse {url}: {e}")

    for game, url in INDEX_URLS.items():
        try:
            index = requests.get(url, timeout=15).json()
            hardcore = index.get("hardcore")
            if hardcore:
                problem = check_hardcore_freshness(hardcore["generated_at"])
                if problem:
                    problems.append(f"{game}: {problem}")
        except Exception as e:
            problems.append(f"could not fetch/parse {url}: {e}")

    try:
        status = requests.get(STATUS_URL, timeout=15).json()
        meta_by_game = status["meta"]
        if status.get("stats_cache_stale"):
            problems.append("status.json: stats_cache_stale is true (the hub itself says its GGG stats table is stale)")
        problems.extend(check_archive(status.get("archive")))
        for game in ("poe2", "poe1"):
            problem = check_meta_freshness(meta_by_game[game]["last_meta"])
            if problem:
                problems.append(f"{game}: {problem}")
            problem = check_meta_sample_total(meta_by_game[game].get("meta_job_summary"))
            if problem:
                problems.append(f"{game}: {problem}")
    except Exception as e:
        problems.append(f"could not fetch/parse {STATUS_URL}: {e}")

    for game, url in SKILLS_INDEX_URLS.items():
        try:
            skills_index = requests.get(url, timeout=15).json()
            problem = check_skills_index_freshness(skills_index["generated_at"])
            if problem:
                problems.append(f"{game}: {problem}")
            problem = check_skills_index_population(skills_index.get("population_total"))
            if problem:
                problems.append(f"{game}: {problem}")
        except Exception as e:
            problems.append(f"could not fetch/parse {url}: {e}")

    # the weekly-refresh artefacts: the stats table, the gems table and the public aliases
    for urls, check in ((STATS_CACHE_URLS, check_stats_cache), (GEMS_URLS, check_gems), (ALIASES_URLS, check_aliases)):
        for game, url in urls.items():
            try:
                problems.extend(check(game, requests.get(url, timeout=30).json()))
            except Exception as e:
                problems.append(f"could not fetch/parse {url}: {e}")

    if problems:
        for p in problems:
            print(f"::error::{p}")
        return 1

    print("OK: prices, news, meta, skills and the weekly stats/gems/alias files (all games) are fresh and non-empty")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
