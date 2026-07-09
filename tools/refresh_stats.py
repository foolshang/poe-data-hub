"""Standalone tool: refresh the GGG trade2/data/stats cache on Firebase.

GGG permanently blocks trade2/data/stats requests from GCP IPs (confirmed
live 2026-07-09 from the production VM -- see CLAUDE.md), so the deploy VM
can't refresh this table itself. This script is meant to run from anywhere
that CAN reach GGG -- a dev machine, or (primarily) the refresh-stats.yml
GitHub Actions workflow -- and publishes the result to Firebase Storage at
internal/stats_cache.json, which ModNormalizer checks first in every
environment (see src/processing/normalizer.py's Firebase -> GGG direct ->
disk cache load order).

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json python -m tools.refresh_stats

Validates the response shape and a minimum total-entry-count floor before
uploading -- a malformed or truncated response must never overwrite a good
cache. Exits non-zero (and uploads nothing) on any fetch or validation
failure, so a scheduled run just shows red in CI rather than corrupting
production data.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any

import requests

from src.storage.publisher import PricesPublisher

log = logging.getLogger("refresh_stats")

STATS_URL = "https://www.pathofexile.com/api/trade2/data/stats"
USER_AGENT = "poe-data-hub-stats-refresh/0.1 (contact: foolshang.live@gmail.com)"
BUCKET_NAME = "poe-data-hub"
BLOB_PATH = "internal/stats_cache.json"
# Sanity floor: the real table has 10k+ entries across all groups. A
# truncated or broken response would be far smaller than this.
MIN_TOTAL_ENTRIES = 5000


def fetch_stats() -> dict[str, Any]:
    resp = requests.get(STATS_URL, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def validate(data: Any) -> int:
    """Returns the total entry count if `data` looks like a real trade2
    stats response; raises ValueError otherwise (caller must not upload)."""
    if not isinstance(data, dict) or not isinstance(data.get("result"), list) or not data["result"]:
        raise ValueError(f"unexpected shape: {type(data).__name__}")
    total = 0
    for group in data["result"]:
        if not isinstance(group.get("id"), str) or not isinstance(group.get("entries"), list):
            raise ValueError(f"malformed group in result: {group!r}")
        total += len(group["entries"])
    if total < MIN_TOTAL_ENTRIES:
        raise ValueError(f"suspiciously low entry count: {total} (expected >= {MIN_TOTAL_ENTRIES})")
    return total


def run() -> int:
    try:
        data = fetch_stats()
    except requests.RequestException as e:
        log.error("fetch failed: %s", e)
        return 1

    try:
        total_entries = validate(data)
    except ValueError as e:
        log.error("validation failed, NOT uploading (keeping whatever's already published): %s", e)
        return 1

    envelope = {"fetched_at": time.time(), "data": data}

    publisher = PricesPublisher(bucket_name=BUCKET_NAME)
    try:
        publisher.publish(envelope, BLOB_PATH, cache_control="no-cache")
    except Exception as e:
        log.error("upload (or post-upload verification) failed: %s", e)
        return 1

    log.info(
        "uploaded trade2 stats to gs://%s/%s: %d groups, %d entries",
        BUCKET_NAME, BLOB_PATH, len(data["result"]), total_entries,
    )
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
