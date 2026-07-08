"""Fetch PoE2 prices from poe2scout.com (spec section 3.2).

Endpoints re-probed 2026-07-08 (samples in data/samples/scout_*.json) because
the shape changed since
D:\\Projects\\PoE_Price-Trade_Checker\\poe_price_trade\\poe2scout_client.py was
last probed (2026-06-26):
  - `Currencies/ByCategory` items dropped `ListingCount` in favor of
    `CurrentQuantity` (still has `CurrentPrice`, still exalted-denominated)
  - `/Items/Categories` (used there for currency-category auto-discovery) now
    404s — dead, no longer usable
  - `/Leagues/{league}/Items` turns out to return EVERY category in one call
    (currency AND unique items — 1275 entries across 26 CategoryApiId values
    in the sample, each with CurrentPrice already in exalted). This replaces
    both the old per-category `Currencies/ByCategory` pagination loop and the
    currency/unique split, in a single request instead of ~15+.
  - `/Leagues` gained `ChaosDivinePrice` (chaos per divine) and an `IsCurrent`
    flag not present in the old client — used here to add a chaos_value
    conversion the old client didn't have.

GET /poe2/Leagues                -> DivinePrice + ChaosDivinePrice per league
GET /poe2/Leagues/{league}/Items -> every currency + unique item, CurrentPrice in exalted
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

_BASE = "https://poe2scout.com/api/poe2"
DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")

# CategoryApiId -> (friendly category name, schema-8.1 bucket). Anything not
# listed here is still published (never silently dropped) under a derived
# name in the "items" bucket, with a log line so it can be added here later.
_CATEGORY_INFO: dict[str, tuple[str, str]] = {
    "currency": ("Currency", "currency"),
    "fragments": ("Fragment", "currency"),
    "runes": ("Rune", "currency"),
    "essences": ("Essence", "currency"),
    "ultimatum": ("SoulCore", "currency"),
    "expedition": ("Artifact", "currency"),
    "ritual": ("Omen", "currency"),
    "vaultkeys": ("Fragment", "currency"),
    "breach": ("Catalyst", "currency"),
    "abyss": ("AbyssalBone", "currency"),
    "uncutgems": ("UncutGem", "currency"),
    "lineagesupportgems": ("SkillGem", "currency"),
    "incursion": ("Artifact", "currency"),
    "idol": ("Idol", "currency"),
    "verisium": ("Verisium", "currency"),
    "vaal": ("Currency", "currency"),
    "delirium": ("Delirium", "currency"),
    "accessory": ("UniqueAccessory", "items"),
    "armour": ("UniqueArmour", "items"),
    "weapon": ("UniqueWeapon", "items"),
    "jewel": ("UniqueJewel", "items"),
    "flask": ("UniqueFlask", "items"),
    "map": ("UniqueMap", "items"),
    "waystones": ("UniqueMap", "items"),
    "sanctum": ("UniqueAccessory", "items"),
    "talismans": ("UniqueAccessory", "items"),
}


def _friendly_category(api_id: str) -> tuple[str, str]:
    if api_id in _CATEGORY_INFO:
        return _CATEGORY_INFO[api_id]
    derived = "Unique" + api_id.replace("_", " ").title().replace(" ", "")
    log.info("scout_prices: unmapped CategoryApiId %r -> derived name %r (items bucket)", api_id, derived)
    return derived, "items"


def category_bucket(category_name: str) -> str:
    for name, bucket in _CATEGORY_INFO.values():
        if name == category_name:
            return bucket
    return "items"


class ScoutPricesFetcher:
    def __init__(self, http_client: HttpClient, failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR):
        self._http = http_client
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)

    def fetch_all(self, league: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        div_price, chaos_div_price = self._fetch_rates(league)
        entries = self._fetch_items(league, div_price, chaos_div_price)
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["category"]] = counts.get(e["category"], 0) + 1
        return entries, counts

    def _fetch_rates(self, league: str) -> tuple[float, Optional[float]]:
        url = f"{_BASE}/Leagues"
        data = self._http.get_json(url)
        if not isinstance(data, list) or not data:
            self._dump_failed_response(data, url, "Leagues")
            raise ValueError(f"scout_prices: Leagues response validation failed ({url})")

        for entry in data:
            if entry.get("Value") == league:
                div_price = float(entry.get("DivinePrice") or 0.0) or 1.0
                chaos_div_price = entry.get("ChaosDivinePrice")
                return div_price, (float(chaos_div_price) if chaos_div_price else None)

        log.warning("scout_prices: league %r not found in /Leagues — divine_value will be unavailable", league)
        return 1.0, None

    def _fetch_items(self, league: str, div_price: float, chaos_div_price: Optional[float]) -> list[dict[str, Any]]:
        league_path = urllib.parse.quote(league, safe="")
        url = f"{_BASE}/Leagues/{league_path}/Items"
        data = self._http.get_json(url)
        if not isinstance(data, list) or not data:
            self._dump_failed_response(data, url, "Items")
            raise ValueError(f"scout_prices: Items response validation failed ({url})")

        entries: list[dict[str, Any]] = []
        for x in data:
            exalted_value = float(x.get("CurrentPrice") or 0)
            name = x.get("Name") or x.get("Text") or ""
            if not name or exalted_value <= 0:
                continue
            cat_api = str(x.get("CategoryApiId") or "").lower()
            if not cat_api:
                continue
            category, _bucket = _friendly_category(cat_api)

            entry: dict[str, Any] = {
                "category": category,
                "name": name,
                "base": x.get("Type") or None,
                "value": exalted_value,
                "value_currency": "exalted",
                "divine_value": exalted_value / div_price,
                "source": "scout",
                "trade_id": x.get("ApiId"),
                "icon_url": x.get("IconUrl"),
            }
            if chaos_div_price:
                entry["chaos_value"] = exalted_value * chaos_div_price / div_price
            entries.append(entry)
        return entries

    def _dump_failed_response(self, data: Any, url: str, label: str) -> None:
        path = self._failed_dir / f"poe2scout.com_{label}_{int(time.time())}.json"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("scout_prices: could not write failed response dump", exc_info=True)
