"""Standalone tool: build and stage the skill/support gem name + metadata
table for both games at `internal/gems_normalized_{game}.json`. Runs
alongside the mod-stats cache refresh, from the same `refresh-stats.yml`
GitHub Actions job.

**Two-hop publish, same reason `internal/stats_cache.json` exists (SPEC.md
8.3)**: this job's GGG-sourced input (`trade(2)/data/items`, see below) is
blocked from GCP IPs, so it has to run here, using the `stats-refresh`
service account -- which is deliberately IAM-restricted to the `internal/`
prefix only (CLAUDE.md), so it *cannot* write the real public
`{game}/gems.json` path directly. This script stages the finished,
already-filtered result at `internal/gems_normalized_{game}.json` instead;
`src/main.py`'s `run_gems_once` (running on the VM, with full-access
credentials) copies that staged file to the public path on its own
schedule. Staging the *normalized* result (not the raw RePoE response)
matters here too: PoE1's raw `gems.json` is ~41MB, and the VM-side hop
must never have to download/parse that (see the VM RAM budget note in
CLAUDE.md) -- all the heavy lifting happens here, on GitHub's runner.

Two independent sources, per game:

- **RePoE fork** (`raw.githubusercontent.com/repoe-fork/{poe1,poe2}`) -- the
  same community datamine PoE_Price-Trade_Checker's `build_mod_meta.py`
  already pulls `base_items.json`/`mods.json` from, just two files neither
  project had touched yet (`skill_gems.json` for poe2, `gems.json` for
  poe1). This is the primary source: full per-gem metadata (display name,
  tags, is_support/gem_type) including, for PoE1, the `discriminator` field
  that identifies transfigured variants (`alt_x`/`alt_y`, e.g. `IceNovaAltX`
  -> "Ice Nova of Frostbolts"). PoE2's RePoE data has zero entries with a
  `discriminator` field at all -- confirmed empirically 2026-09-15, not a
  gap: PoE2 has no transfigured-gem mechanic in the current game.
  `raw.githubusercontent.com` is not one of GGG's blocked endpoints (see
  CLAUDE.md) -- reachable from anywhere, including the deploy VM -- but
  fetched here anyway to keep gem-table assembly in one place alongside the
  GGG-sourced cross-check below.
- **GGG's own `trade(2)/data/items` "Gems" category** -- confirmed live
  2026-09-15 blocked from GCP IPs the same way `trade(2)/data/stats` is
  (200 OK from a dev machine, would 403 from the VM), hence routed through
  GitHub Actions. Name-only list, no metadata (`{"result": [{"label":
  "Gems", "entries": [{"type": "<name>"}, ...]}]}`) -- used *only* as a
  `verified_tradeable` cross-check flag, never as a filter: confirmed live
  that this list simply does not expose transfigured gem names at all
  (0/217 PoE1 transfigured display names from RePoE matched here), so
  treating "absent from trade list" as "drop it" would silently delete
  every transfigured gem PoE1 has. Every gem RePoE reports is kept in the
  output; trade-list absence only ever shows up as
  `verified_tradeable: false` for a human to look at.

Deliberately excludes RePoE's per-gem `per_level`/`stat_conversions`/tooltip
bulk (PoE1's raw `gems.json` is ~41MB with it) -- out of scope for
skill/gear/jewel meta normalization (that's about which mods/passives a
skill's users run, not the gem's own level-up numbers), and republishing
that much data on a weekly cron would reproduce the exact Firebase egress
cost problem already hit once by prices (see config.yaml's
`schedule.prices_interval_minutes` comment). Only normalization-relevant
fields are kept in the published output; the full raw response is fetched
transiently to build it and never itself uploaded.

Each game's RePoE-fetch/normalize/publish runs independently in its own
try/except -- one game's failure can't corrupt or block the other's
(same pattern as tools/refresh_stats.py). Trade-items fetch failure is
non-fatal per game: `verified_tradeable` degrades to `None` (unknown)
for every gem rather than aborting that game's whole publish, per spec
10.1b (never crash the job on a bad/empty response).

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json python -m tools.refresh_gems
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

from src.storage.publisher import PricesPublisher

log = logging.getLogger("refresh_gems")

USER_AGENT = "poe-data-hub-stats-refresh/0.1 (contact: foolshang.live@gmail.com)"
BUCKET_NAME = "poe-data-hub"
GEMS_SCHEMA_VERSION = 1

FAILED_RESPONSES_DIR = Path("data/failed_responses")

REPOE_RAW_TMPL = "https://raw.githubusercontent.com/repoe-fork/{repo}/master/data/{file}"
TRADE_ITEMS_URL = {
    "poe2": "https://www.pathofexile.com/api/trade2/data/items",
    "poe1": "https://www.pathofexile.com/api/trade/data/items",
}
REPOE_GEM_FILE = {
    "poe2": ("poe2", "skill_gems.json"),
    "poe1": ("poe1", "gems.json"),
}

DNT_PREFIXES = ("[DNT]", "[DNT-UNUSED]")
ROYALE_SUFFIX = "Royale"  # defunct PvP-mode gem duplicates (poe1 only) -- not real obtainable gems
ALT_SUFFIXES = ("AltX", "AltY")  # poe1 transfigured-variant key suffixes

# Sanity floors -- live counts 2026-09-15 (after junk filtering) were
# poe2=1163/1191, poe1=915/1458. Kept comfortably below to tolerate normal
# datamine growth/shrink without a real regression tripping this.
MIN_GEMS = {"poe2": 900, "poe1": 700}


@dataclass(frozen=True)
class GemsTarget:
    game: str
    blob_path: str


TARGETS: list[GemsTarget] = [
    GemsTarget("poe2", "internal/gems_normalized_poe2.json"),
    GemsTarget("poe1", "internal/gems_normalized_poe1.json"),
]


def fetch_json(url: str, timeout: float = 60.0) -> Any:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _dump_failed_response(data: Any, label: str) -> None:
    FAILED_RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    path = FAILED_RESPONSES_DIR / f"{label}_{int(time.time())}.json"
    try:
        import json

        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError) as e:
        log.warning("refresh_gems: could not dump failed response to %s: %s", path, e)


def _is_junk_name(name: Optional[str]) -> bool:
    return not name or any(name.startswith(p) for p in DNT_PREFIXES)


def _base_key(gem_id: str) -> str:
    for suffix in ALT_SUFFIXES:
        if gem_id.endswith(suffix):
            return gem_id[: -len(suffix)]
    return gem_id


def trade_gem_names(trade_items: Any) -> set[str]:
    """Raises ValueError if the response doesn't look like a real
    trade(2)/data/items payload -- caller must not treat a malformed
    response as "no gems are tradeable"."""
    if not isinstance(trade_items, dict) or not isinstance(trade_items.get("result"), list):
        raise ValueError(f"unexpected trade/data/items shape: {type(trade_items).__name__}")
    names: set[str] = set()
    found_gems_group = False
    for group in trade_items["result"]:
        if group.get("label") == "Gems":
            found_gems_group = True
            for e in group.get("entries", []):
                t = e.get("type")
                if t:
                    names.add(t)
    if not found_gems_group:
        raise ValueError("trade/data/items response has no 'Gems' label group")
    return names


def normalize_poe2(repoe_data: dict, trade_names: Optional[set[str]]) -> list[dict[str, Any]]:
    out = []
    for gem_id, v in repoe_data.items():
        base = v.get("base_item") or {}
        name = base.get("display_name")
        if _is_junk_name(name):
            continue
        out.append(
            {
                "id": gem_id,
                "display_name": name,
                "is_support": v.get("gem_type") == "support",
                "gem_type": v.get("gem_type"),
                "tags": sorted(v.get("tags") or []),
                "transfigured_of": None,  # PoE2 has no transfigured-gem mechanic in current data
                "is_lineage": bool(v.get("is_lineage")),
                "verified_tradeable": None if trade_names is None else name in trade_names,
            }
        )
    return out


def normalize_poe1(repoe_data: dict, trade_names: Optional[set[str]]) -> list[dict[str, Any]]:
    # Base (non-transfigured) gems' display names, keyed by their raw RePoE
    # id, so alt_x/alt_y entries can record which base skill they transfigure.
    base_name_by_id: dict[str, str] = {}
    for gem_id, v in repoe_data.items():
        if v.get("discriminator") or gem_id.endswith(ROYALE_SUFFIX):
            continue
        name = v.get("display_name") or (v.get("base_item") or {}).get("display_name")
        if name:
            base_name_by_id[gem_id] = name

    out = []
    for gem_id, v in repoe_data.items():
        if gem_id.endswith(ROYALE_SUFFIX):
            continue  # defunct PvP-mode duplicate, not a real obtainable gem
        name = v.get("display_name") or (v.get("base_item") or {}).get("display_name")
        if _is_junk_name(name):
            continue
        disc = v.get("discriminator")
        transfigured_of = base_name_by_id.get(_base_key(gem_id)) if disc else None
        out.append(
            {
                "id": gem_id,
                "display_name": name,
                "is_support": bool(v.get("is_support")),
                "gem_type": None,  # poe1 RePoE data has no gem_type field, only is_support
                "tags": sorted(v.get("tags") or []),
                "transfigured_of": transfigured_of,
                "is_lineage": False,  # poe1 has no Lineage-gem mechanic
                "verified_tradeable": None if trade_names is None else name in trade_names,
            }
        )
    return out


NORMALIZERS = {"poe2": normalize_poe2, "poe1": normalize_poe1}


def build_gems_schema(gems: list[dict[str, Any]]) -> dict[str, Any]:
    from datetime import datetime, timezone

    return {
        "schema_version": GEMS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gems": gems,
    }


def refresh_one(publisher: PricesPublisher, target: GemsTarget) -> bool:
    """Returns True on success. Never raises -- logs and returns False so
    one game's failure doesn't stop the other's refresh."""
    repo, fname = REPOE_GEM_FILE[target.game]
    repoe_url = REPOE_RAW_TMPL.format(repo=repo, file=fname)

    try:
        repoe_data = fetch_json(repoe_url, timeout=120.0)
    except requests.RequestException as e:
        log.error("%s: RePoE fetch failed: %s", target.game, e)
        return False

    if not isinstance(repoe_data, dict) or not repoe_data:
        _dump_failed_response(repoe_data, f"repoe_{target.game}_gems")
        log.error("%s: RePoE response validation failed (empty/non-dict), NOT uploading", target.game)
        return False

    trade_names: Optional[set[str]] = None
    try:
        trade_raw = fetch_json(TRADE_ITEMS_URL[target.game])
        trade_names = trade_gem_names(trade_raw)
    except (requests.RequestException, ValueError) as e:
        log.warning(
            "%s: trade/data/items cross-check unavailable (%s) -- publishing with verified_tradeable=null",
            target.game,
            e,
        )
        if isinstance(e, ValueError):
            _dump_failed_response(trade_raw, f"trade_items_{target.game}")

    gems = NORMALIZERS[target.game](repoe_data, trade_names)

    if len(gems) < MIN_GEMS[target.game]:
        _dump_failed_response(repoe_data, f"repoe_{target.game}_gems")
        log.error(
            "%s: suspiciously low gem count after filtering: %d (expected >= %d), NOT uploading",
            target.game,
            len(gems),
            MIN_GEMS[target.game],
        )
        return False

    envelope = build_gems_schema(gems)

    try:
        publisher.publish(envelope, target.blob_path, cache_control="no-cache")
    except Exception as e:
        log.error("%s: upload (or post-upload verification) failed: %s", target.game, e)
        return False

    log.info(
        "%s: staged gems table to gs://%s/%s: %d gems (public copy published separately by run_gems_once on the VM)",
        target.game, BUCKET_NAME, target.blob_path, len(gems),
    )
    return True


def run() -> int:
    publisher = PricesPublisher(bucket_name=BUCKET_NAME)
    results = {target.game: refresh_one(publisher, target) for target in TARGETS}
    if not all(results.values()):
        failed = [game for game, ok in results.items() if not ok]
        log.error("gems refresh failed for: %s", ", ".join(failed))
        return 1
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
