"""Standalone tool: regenerate the RePoE-based rendering aliases and publish them to Firebase.

ModNormalizer uses the published file as its very last rule (src/processing/rendering_aliases.py explains what
the aliases are and why they are read from the game's own translations instead of guessed from text).

Runs in GitHub Actions (refresh-stats.yml), not on the VM: PoE1's stat_translations.json is ~12MB and the VM has
no RAM to spare (CLAUDE.md, "VM RAM budget"); the VM only ever downloads the small result. Inputs:
  * RePoE fork (raw.githubusercontent.com/repoe-fork/{poe1,poe2}) -- community datamine, not a GGG endpoint
  * the GGG stats table, read from the copy refresh_stats.py already published (internal/stats_cache*.json)
Outputs, both under internal/ (all the scoped service account may touch):
  internal/rendering_aliases_{game}.json            current
  internal/rendering_aliases_{game}.last_good.json  the file that was current BEFORE the last successful publish
  internal/aliases_public_{game}.json               the readable PUBLIC contract (SPEC.md 8.12), staged: the VM's
                                                    run_aliases_once copies it to {game}/aliases/latest.json (this
                                                    scoped service account cannot write public paths)

Guard: nothing is published over a good file when the new payload is empty or has far fewer aliases / deprecated
ids than the one already published (a RePoE schema change or a truncated download must show up as a red run, not
as silently lost aliases). Exits non-zero on any failure; one game's failure never blocks the other.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json python -m tools.refresh_rendering_aliases
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable

import requests

from src.processing.rendering_aliases import KINDS, PUBLIC_KINDS, build_aliases, build_stat_groups, check_not_shrunk, to_public
from src.storage.publisher import PricesPublisher
from src.utils.http_client import build_user_agent

log = logging.getLogger("refresh_rendering_aliases")

USER_AGENT = build_user_agent("poe-data-hub-aliases-refresh/0.1")
BUCKET_NAME = "poe-data-hub"
PUBLIC = f"https://storage.googleapis.com/{BUCKET_NAME}"
# RePoE's PoE1 file has ~11k entries (checked 2026-10-19), PoE2's ~10.7k; a response far below that is broken.
MIN_TRANSLATIONS = 5000
MIN_TABLE_ENTRIES = 3000


@dataclass(frozen=True)
class Target:
    game: str
    repoe_url: str
    stats_url: str
    blob_path: str
    last_good_path: str
    public_staged_path: str
    # PoE2's translations carry GGG trade ids on only ~292 of ~10.7k entries (2026-10-19), so an empty result is not
    # an error there; for PoE1 it is.
    min_aliases: int


TARGETS: list[Target] = [
    Target("poe1", "https://raw.githubusercontent.com/repoe-fork/poe1/master/data/stat_translations.json",
           f"{PUBLIC}/internal/stats_cache_poe1.json", "internal/rendering_aliases_poe1.json",
           "internal/rendering_aliases_poe1.last_good.json", "internal/aliases_public_poe1.json", 1),
    Target("poe2", "https://raw.githubusercontent.com/repoe-fork/poe2/master/data/stat_translations/stat_descriptions.json",
           f"{PUBLIC}/internal/stats_cache.json", "internal/rendering_aliases_poe2.json",
           "internal/rendering_aliases_poe2.last_good.json", "internal/aliases_public_poe2.json", 0),
]


def fetch_json(url: str) -> Any:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=180)
    resp.raise_for_status()
    return resp.json()


def table_from_stats(envelope: Any, kinds: tuple[str, ...] = KINDS) -> dict[str, dict[str, str]]:
    """{kind: {GGG stat id: text}} for `kinds` from the published stats cache envelope; raises ValueError if it is not one."""
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise ValueError("unexpected stats cache shape")
    table: dict[str, dict[str, str]] = {}
    for group in data["result"]:
        if group.get("id") in kinds:
            table[group["id"]] = {e["id"]: e["text"] for e in group.get("entries", []) if e.get("id") and e.get("text")}
    if sum(len(v) for v in table.values()) < MIN_TABLE_ENTRIES:
        raise ValueError("suspiciously small GGG stats table")
    return table


def refresh_one(publisher: PricesPublisher, target: Target, fetch: Callable[[str], Any] = fetch_json) -> bool:
    """True on success. Never raises: logs and returns False so one game cannot stop the other."""
    try:
        translations = fetch(target.repoe_url)
        if not isinstance(translations, list) or len(translations) < MIN_TRANSLATIONS:
            raise ValueError(f"unexpected RePoE translations: {type(translations).__name__} / {len(translations) if hasattr(translations, '__len__') else '?'}")
        stats_envelope = fetch(target.stats_url)
        table = table_from_stats(stats_envelope)
        payload = build_aliases(target.game, translations, table)
        previous = publisher.fetch_previous(target.blob_path)
        check_not_shrunk(payload, previous, min_aliases=target.min_aliases)
        # the PUBLIC contract covers every item mod kind; the hub-private payload above stays on the 3 kinds the running
        # hub reads (its AliasIndex and normalizer know nothing else)
        public_table = table_from_stats(stats_envelope, PUBLIC_KINDS)
        public_payload = build_aliases(target.game, translations, public_table, PUBLIC_KINDS)
        public = to_public(public_payload, build_stat_groups(public_table, PUBLIC_KINDS), target.repoe_url)
    except Exception as e:
        log.error("%s: NOT publishing (keeping whatever is already there): %s", target.game, e)
        return False
    try:
        if previous:
            publisher.publish(previous, target.last_good_path, cache_control="no-cache")
        publisher.publish(payload, target.blob_path, cache_control="no-cache")
        publisher.publish(public, target.public_staged_path, cache_control="no-cache")
    except Exception as e:
        log.error("%s: upload (or post-upload verification) failed: %s", target.game, e)
        return False
    log.info("%s: published %d aliases, %d deprecated-only ids, %d stat groups to gs://%s/%s and the public contract to %s (stats: %s)",
             target.game, len(payload["aliases"]), len(payload["deprecated"]), len(public["stat_groups"]), BUCKET_NAME,
             target.blob_path, target.public_staged_path, payload["stats"])
    return True


def run() -> int:
    publisher = PricesPublisher(bucket_name=BUCKET_NAME)
    results = {t.game: refresh_one(publisher, t) for t in TARGETS}
    failed = [g for g, ok in results.items() if not ok]
    if failed:
        log.error("rendering alias refresh failed for: %s", ", ".join(failed))
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
