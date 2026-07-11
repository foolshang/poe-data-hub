"""Layer-2 monitoring (external, independent of Layer 1): fetches
poe2/prices/latest.json and status.json from the *public* Firebase URLs (no
credentials needed) and fails (non-zero exit) if either looks stale.

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

PRICES_URL = "https://storage.googleapis.com/poe-data-hub/poe2/prices/latest.json"
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


def run() -> int:
    problems: list[str] = []

    try:
        prices = requests.get(PRICES_URL, timeout=15).json()
        problem = check_prices_freshness(prices["generated_at"])
        if problem:
            problems.append(problem)
    except Exception as e:
        problems.append(f"could not fetch/parse {PRICES_URL}: {e}")

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

    print("OK: prices and meta are both fresh")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
