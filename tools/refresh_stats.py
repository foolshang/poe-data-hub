"""Standalone tool: refresh GGG's mod-stats cache for both games on Firebase.

GGG permanently blocks trade2/data/stats requests from GCP IPs (confirmed
live 2026-07-09 from the production VM -- see CLAUDE.md), so the deploy VM
can't refresh this table itself. This script is meant to run from anywhere
that CAN reach GGG -- a dev machine, or (primarily) the refresh-stats.yml
GitHub Actions workflow -- and publishes the result to Firebase Storage,
which ModNormalizer checks first in every environment (see
src/processing/normalizer.py's Firebase -> GGG direct -> disk cache load
order).

Phase 10: PoE1 uses `trade/data/stats` (not `trade2`) -- a different GGG
endpoint, published to its own Firebase path so PoE1 and PoE2 mod
normalization never share a cache. Also confirmed permanently blocked from
GCP IPs (2026-07-15, the hard way -- see CLAUDE.md's "Deploy gotcha"), so
it's routed through the same GitHub-Actions path as trade2.
Each game is fetched/validated/published independently -- one game's
failure must never block or corrupt the other's cache.

IMPORTANT: after adding a new game/target here, its Firebase blob doesn't
exist until this workflow actually runs -- trigger `gh workflow run
refresh-stats.yml` manually rather than waiting for the weekly cron, or
that game's meta job will fail on its first VM run (Firebase 404 -> GGG
direct 403 -> empty disk cache -> raises).

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json python -m tools.refresh_stats

Validates the response shape and a minimum total-entry-count floor before
uploading -- a malformed or truncated response must never overwrite a good
cache. Exits non-zero (and uploads nothing for the failing game(s)) on any
fetch or validation failure, so a scheduled run just shows red in CI rather
than corrupting production data.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

import requests

from src.storage.publisher import PricesPublisher

log = logging.getLogger("refresh_stats")

USER_AGENT = "poe-data-hub-stats-refresh/0.1 (contact: foolshang.live@gmail.com)"
BUCKET_NAME = "poe-data-hub"
# Sanity floor: the real tables have thousands of entries across all groups.
# A truncated or broken response would be far smaller than either game's
# floor -- PoE1's trade/data/stats has ~15.5k entries live-checked
# 2026-07-14, PoE2's trade2 has 10k+; both floors kept well below their
# real live count so routine table growth never trips this.
MIN_TOTAL_ENTRIES = 5000


@dataclass(frozen=True)
class StatsTarget:
    game: str
    stats_url: str
    blob_path: str


TARGETS: list[StatsTarget] = [
    StatsTarget("poe2", "https://www.pathofexile.com/api/trade2/data/stats", "internal/stats_cache.json"),
    StatsTarget("poe1", "https://www.pathofexile.com/api/trade/data/stats", "internal/stats_cache_poe1.json"),
]


def fetch_stats(stats_url: str) -> dict[str, Any]:
    resp = requests.get(stats_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def validate(data: Any) -> int:
    """Returns the total entry count if `data` looks like a real trade(2)
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


def refresh_one(publisher: PricesPublisher, target: StatsTarget) -> bool:
    """Returns True on success. Never raises -- logs and returns False so
    one game's failure doesn't stop the other's refresh."""
    import time

    try:
        data = fetch_stats(target.stats_url)
    except requests.RequestException as e:
        log.error("%s: fetch failed: %s", target.game, e)
        return False

    try:
        total_entries = validate(data)
    except ValueError as e:
        log.error("%s: validation failed, NOT uploading (keeping whatever's already published): %s", target.game, e)
        return False

    envelope = {"fetched_at": time.time(), "data": data}

    try:
        publisher.publish(envelope, target.blob_path, cache_control="no-cache")
    except Exception as e:
        log.error("%s: upload (or post-upload verification) failed: %s", target.game, e)
        return False

    log.info(
        "%s: uploaded stats to gs://%s/%s: %d groups, %d entries",
        target.game, BUCKET_NAME, target.blob_path, len(data["result"]), total_entries,
    )
    return True


def run() -> int:
    publisher = PricesPublisher(bucket_name=BUCKET_NAME)
    results = {target.game: refresh_one(publisher, target) for target in TARGETS}
    if not all(results.values()):
        failed = [game for game, ok in results.items() if not ok]
        log.error("stats refresh failed for: %s", ", ".join(failed))
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
