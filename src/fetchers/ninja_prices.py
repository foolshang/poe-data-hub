"""Fetch PoE2 prices from poe.ninja (spec section 3.1).

Endpoints confirmed live 2026-07-08 (samples saved to data/samples/):
  GET https://poe.ninja/poe2/api/economy/exchange/current/overview?league=..&type=..
      -> currency + misc "GENERAL" categories (Currency, Fragments, Runes, ...)
  GET https://poe.ninja/poe2/api/economy/stash/current/item/overview?league=..&type=..
      -> unique/equipment categories (UniqueWeapons, UniqueArmours, ...)

Both return the same JSON shape:
  {"core": {"items": [...], "rates": {"exalted": ex_per_div, "chaos": chaos_per_div, ...},
            "primary": "divine", "secondary": ...},
   "lines": [{"id"/"itemId": ..., "name"/"baseType": ..., "primaryValue": <divine>, ...}],
   "items": [{"id": ..., "name": ..., "detailsId": ..., "icon": ...}]}

primaryValue is always denominated in `core.primary` (divine, on every
category probed so far). We convert it into every unit `core.rates` actually
exposes (currently exalted and chaos) instead of picking just one, per the
"never silently drop a price unit" rule.

Category list ported from
D:\\Projects\\PoE_Price-Trade_Checker\\poe_price_trade\\profiles.py POE2_PROFILE.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

_BASE_EXCHANGE = "https://poe.ninja/poe2/api/economy/exchange/current/overview"
_BASE_STASH = "https://poe.ninja/poe2/api/economy/stash/current/item/overview"

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")


@dataclass
class CategoryConfig:
    name: str
    endpoint: str  # "exchange" (-> schema "currency" bucket) | "stash" (-> schema "items" bucket)
    api_type: str


CATEGORIES: list[CategoryConfig] = [
    # GENERAL (exchange_overview) -> published under "currency"
    CategoryConfig("Currency", "exchange", "Currency"),
    CategoryConfig("Fragment", "exchange", "Fragments"),
    CategoryConfig("AbyssalBone", "exchange", "Abyss"),
    CategoryConfig("UncutGem", "exchange", "UncutGems"),
    CategoryConfig("LineageGem", "exchange", "LineageSupportGems"),
    CategoryConfig("Essence", "exchange", "Essences"),
    CategoryConfig("SoulCore", "exchange", "SoulCores"),
    CategoryConfig("Idol", "exchange", "Idols"),
    CategoryConfig("Rune", "exchange", "Runes"),
    CategoryConfig("Omen", "exchange", "Ritual"),
    CategoryConfig("Expedition", "exchange", "Expedition"),
    CategoryConfig("Delirium", "exchange", "Delirium"),
    CategoryConfig("Catalyst", "exchange", "Breach"),
    CategoryConfig("Verisium", "exchange", "Verisium"),
    # EQUIPMENT / ATLAS (stash_overview) -> published under "items"
    CategoryConfig("UniqueWeapon", "stash", "UniqueWeapons"),
    CategoryConfig("UniqueArmour", "stash", "UniqueArmours"),
    CategoryConfig("UniqueAccessory", "stash", "UniqueAccessories"),
    CategoryConfig("UniqueFlask", "stash", "UniqueFlasks"),
    CategoryConfig("UniqueCharm", "stash", "UniqueCharms"),
    CategoryConfig("UniqueJewel", "stash", "UniqueJewels"),
    CategoryConfig("UniqueRelic", "stash", "UniqueSanctumRelics"),
    CategoryConfig("UniqueTablet", "stash", "UniqueTablets"),
    CategoryConfig("PrecursorTablet", "stash", "PrecursorTablets"),
]

_CATEGORY_BUCKET: dict[str, str] = {
    c.name: ("currency" if c.endpoint == "exchange" else "items") for c in CATEGORIES
}


def category_bucket(category_name: str) -> str:
    """Which top-level schema-8.1 array (`currency` or `items`) a category belongs in."""
    return _CATEGORY_BUCKET.get(category_name, "items")


def _url_for(cat: CategoryConfig, league: str) -> str:
    base = _BASE_EXCHANGE if cat.endpoint == "exchange" else _BASE_STASH
    return f"{base}?league={urllib.parse.quote(league)}&type={cat.api_type}"


class NinjaPricesFetcher:
    def __init__(self, http_client: HttpClient, failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR):
        self._http = http_client
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)

    def fetch_category(self, league: str, cat: CategoryConfig) -> list[dict[str, Any]]:
        url = _url_for(cat, league)
        data = self._http.get_json(url)
        self._validate(data, url, cat.name)
        return self._parse_overview(data, cat.name)

    def fetch_all(self, league: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        entries: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for cat in CATEGORIES:
            try:
                batch = self.fetch_category(league, cat)
                counts[cat.name] = len(batch)
                entries.extend(batch)
            except Exception as e:
                counts[cat.name] = 0
                log.warning("ninja_prices: skip category %s: %s", cat.name, e)
        return entries, counts

    def _validate(self, data: Any, url: str, category: str) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
            self._dump_failed_response(data, url, category)
            raise ValueError(f"ninja_prices: response validation failed for {category} ({url})")
        if len(data["lines"]) == 0:
            log.warning(
                "ninja_prices: %s returned 0 lines with a valid response shape "
                "(possibly empty category or a source-side format change)",
                category,
            )

    def _dump_failed_response(self, data: Any, url: str, category: str) -> None:
        path = self._failed_dir / f"poe.ninja_{category}_{int(time.time())}.json"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("ninja_prices: could not write failed response dump", exc_info=True)

    def _parse_overview(self, data: dict[str, Any], category: str) -> list[dict[str, Any]]:
        core = data.get("core", {}) or {}
        rates: dict[str, float] = core.get("rates", {}) or {}
        primary_unit = core.get("primary", "divine")

        id_to_item: dict[str, dict[str, Any]] = {}
        for it in data.get("items", []) or []:
            if it.get("id"):
                id_to_item[it["id"]] = it

        entries: list[dict[str, Any]] = []
        for ln in data.get("lines", []) or []:
            primary_value = float(ln.get("primaryValue", 0) or 0)
            if primary_value <= 0:
                continue

            line_id = ln.get("id") or ln.get("itemId")
            item = id_to_item.get(line_id, {}) if line_id is not None else {}
            name = ln.get("name") or item.get("name") or str(line_id or "")
            if not name:
                continue

            entry = self._build_entry(ln, item, category, name, primary_value, primary_unit, rates)
            entries.append(entry)

        return entries

    @staticmethod
    def _build_entry(
        ln: dict[str, Any],
        item: dict[str, Any],
        category: str,
        name: str,
        primary_value: float,
        primary_unit: str,
        rates: dict[str, float],
    ) -> dict[str, Any]:
        def convert(unit: str) -> Optional[float]:
            if primary_unit == unit:
                return primary_value
            if primary_unit == "divine" and unit in rates:
                return primary_value * rates[unit]
            return None

        divine_value = convert("divine")
        exalted_value = convert("exalted")
        chaos_value = convert("chaos")

        entry: dict[str, Any] = {
            "category": category,
            "name": name,
            "base": ln.get("baseType") or None,
            "value": exalted_value if exalted_value is not None else primary_value,
            "value_currency": "exalted" if exalted_value is not None else primary_unit,
            "divine_value": divine_value,
            "source": "ninja",
            "listing_count": int(ln.get("listingCount") or ln.get("volumePrimaryValue") or 0),
            "trade_id": ln.get("detailsId") or item.get("detailsId") or ln.get("id"),
            "icon_url": ln.get("icon") or item.get("image"),
        }
        if chaos_value is not None:
            entry["chaos_value"] = chaos_value
        return entry
