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
STATUS_URL = "https://storage.googleapis.com/poe-data-hub/status.json"

PRICES_MAX_AGE_MINUTES = 45
META_MAX_AGE_HOURS = 26


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_prices_freshness(generated_at: str, now: Optional[datetime] = None) -> Optional[str]:
    """Returns a problem description if stale, None if fresh."""
    now = now or datetime.now(timezone.utc)
    age_minutes = (now - _parse_iso(generated_at)).total_seconds() / 60
    if age_minutes > PRICES_MAX_AGE_MINUTES:
        return f"prices/latest.json generated_at is {age_minutes:.0f} minutes old (> {PRICES_MAX_AGE_MINUTES})"
    return None


def check_meta_freshness(last_meta: str, now: Optional[datetime] = None) -> Optional[str]:
    """Returns a problem description if stale, None if fresh."""
    now = now or datetime.now(timezone.utc)
    age_hours = (now - _parse_iso(last_meta)).total_seconds() / 3600
    if age_hours > META_MAX_AGE_HOURS:
        return f"status.json last_meta is {age_hours:.1f} hours old (> {META_MAX_AGE_HOURS})"
    return None


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

    try:
        status = requests.get(STATUS_URL, timeout=15).json()
        problem = check_meta_freshness(status["last_meta"])
        if problem:
            problems.append(problem)
    except Exception as e:
        problems.append(f"could not fetch/parse {STATUS_URL}: {e}")

    if problems:
        for p in problems:
            print(f"::error::{p}")
        return 1

    print("OK: prices (all games) and meta are fresh")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
