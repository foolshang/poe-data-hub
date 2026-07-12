"""Fetch prices from poe.ninja for both PoE1 and PoE2 (spec 3.1, phase 7).

Endpoints confirmed live (samples saved to data/samples/):
  GET https://poe.ninja/{game}/api/economy/exchange/current/overview?league=..&type=..
      -> currency + misc "GENERAL" categories (Currency, Fragments, Runes, ...)
  GET https://poe.ninja/{game}/api/economy/stash/current/item/overview?league=..&type=..
      -> unique/equipment categories (UniqueWeapons, UniqueArmours, ...)
  where {game} is "poe1" or "poe2".

PoE1 and PoE2 share the same URL shape but their *stash* responses differ in
a way that matters (confirmed live 2026-07-11, data/samples/ninja_poe1_stash_uniqueweapon.json
vs ninja_poe2_stash_uniqueweapons.json):
  - PoE2 stash lines carry only `primaryValue`, converted via `core.rates`
    (primary unit "divine", `core` present) -- same pattern as both games'
    exchange endpoint.
  - PoE1 stash lines carry `chaosValue` / `exaltedValue` / `divineValue`
    directly, no `core` object at all. No rate conversion needed there.

Per spec 8.1, PoE2 prices are denominated in "exalted" (falling back to
whatever the primary unit is if no exalted rate exists) while PoE1 prices
are always denominated in "chaos" -- PoE1's exchange endpoint's own primary
unit is "chaos" already (unlike PoE2's "divine"), so this falls out
naturally for that endpoint; the stash endpoint's direct `chaosValue` is
used as-is for the same reason.

Every entry keeps every price unit the source actually provides (never
silently dropped, per CLAUDE.md) as extra fields beyond the required
value/value_currency/divine_value trio.
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

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")


@dataclass
class CategoryConfig:
    name: str
    endpoint: str  # "exchange" (-> schema "currency" bucket) | "stash" (-> schema "items" bucket)
    api_type: str


# Category list ported from
# D:\Projects\PoE_Price-Trade_Checker\poe_price_trade\profiles.py POE2_PROFILE.
POE2_CATEGORIES: list[CategoryConfig] = [
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

# Ported from D:\Projects\Bot-Poe-Discord\poe-discord-bot\price.py
# POE1_CATEGORIES (already live-validated there 2026-07-05); re-confirmed
# live here 2026-07-11 for this project's own samples.
POE1_CATEGORIES: list[CategoryConfig] = [
    CategoryConfig("Currency", "exchange", "Currency"),
    CategoryConfig("Fragment", "exchange", "Fragment"),
    CategoryConfig("DivinationCard", "exchange", "DivinationCard"),
    CategoryConfig("UniqueWeapon", "stash", "UniqueWeapon"),
    CategoryConfig("UniqueArmour", "stash", "UniqueArmour"),
    CategoryConfig("UniqueAccessory", "stash", "UniqueAccessory"),
]

CATEGORIES_BY_GAME: dict[str, list[CategoryConfig]] = {"poe2": POE2_CATEGORIES, "poe1": POE1_CATEGORIES}


def category_bucket(category_name: str, game: str = "poe2") -> str:
    """Which top-level schema-8.1 array (`currency` or `items`) a category belongs in."""
    for c in CATEGORIES_BY_GAME.get(game, POE2_CATEGORIES):
        if c.name == category_name:
            return "currency" if c.endpoint == "exchange" else "items"
    return "items"


def _url_for(game: str, cat: CategoryConfig, league: str) -> str:
    base = (
        f"https://poe.ninja/{game}/api/economy/exchange/current/overview"
        if cat.endpoint == "exchange"
        else f"https://poe.ninja/{game}/api/economy/stash/current/item/overview"
    )
    return f"{base}?league={urllib.parse.quote(league)}&type={cat.api_type}"


class NinjaPricesFetcher:
    def __init__(
        self,
        http_client: HttpClient,
        game: str = "poe2",
        failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR,
    ):
        if game not in CATEGORIES_BY_GAME:
            raise ValueError(f"ninja_prices: unknown game {game!r}")
        self._http = http_client
        self._game = game
        self._categories = CATEGORIES_BY_GAME[game]
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)

    def fetch_category(self, league: str, cat: CategoryConfig) -> list[dict[str, Any]]:
        url = _url_for(self._game, cat, league)
        data = self._http.get_json(url)
        self._validate(data, url, cat.name)
        return self._parse_overview(data, cat)

    def fetch_all(self, league: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Returns (entries, per_category) where per_category is
        {category_name: {"ok": bool, "count": int}} for every configured
        category -- published verbatim as sample.per_category.ninja
        (spec 8.1) so a whole-source "ok" status can't mask a partial
        degradation (e.g. 12/23 categories silently failing while the
        source as a whole still reports "ok"). "ok": False means this
        category's fetch/validate raised; "ok": True with "count": 0 means
        it fetched fine but the source legitimately returned zero lines."""
        entries: list[dict[str, Any]] = []
        per_category: dict[str, dict[str, Any]] = {}
        for cat in self._categories:
            try:
                batch = self.fetch_category(league, cat)
                per_category[cat.name] = {"ok": True, "count": len(batch)}
                entries.extend(batch)
            except Exception as e:
                per_category[cat.name] = {"ok": False, "count": 0}
                log.warning("ninja_prices(%s): skip category %s: %s", self._game, cat.name, e)
        return entries, per_category

    def _validate(self, data: Any, url: str, category: str) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
            self._dump_failed_response(data, url, category)
            raise ValueError(f"ninja_prices({self._game}): response validation failed for {category} ({url})")
        if len(data["lines"]) == 0:
            log.warning(
                "ninja_prices(%s): %s returned 0 lines with a valid response shape "
                "(possibly empty category or a source-side format change)",
                self._game, category,
            )

    def _dump_failed_response(self, data: Any, url: str, category: str) -> None:
        path = self._failed_dir / f"poe.ninja_{self._game}_{category}_{int(time.time())}.json"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("ninja_prices: could not write failed response dump", exc_info=True)

    def _parse_overview(self, data: dict[str, Any], cat: CategoryConfig) -> list[dict[str, Any]]:
        if self._game == "poe1" and cat.endpoint == "stash":
            return self._parse_poe1_stash_direct(data, cat.name)
        return self._parse_rate_converted(data, cat.name)

    def _parse_poe1_stash_direct(self, data: dict[str, Any], category: str) -> list[dict[str, Any]]:
        """PoE1 stash_overview lines already carry chaosValue/exaltedValue/
        divineValue directly -- no core.rates conversion table exists here."""
        entries: list[dict[str, Any]] = []
        for ln in data.get("lines", []) or []:
            chaos_value = float(ln.get("chaosValue", 0) or 0)
            if chaos_value <= 0:
                continue
            name = ln.get("name") or ""
            if not name:
                continue
            entry: dict[str, Any] = {
                "category": category,
                "name": name,
                "base": ln.get("baseType") or None,
                "value": chaos_value,
                "value_currency": "chaos",
                "divine_value": float(ln["divineValue"]) if ln.get("divineValue") is not None else None,
                "source": "ninja",
                "listing_count": int(ln.get("listingCount") or ln.get("count") or 0),
                "trade_id": ln.get("detailsId") or str(ln.get("id") or ""),
                "icon_url": ln.get("icon"),
            }
            if ln.get("exaltedValue") is not None:
                entry["exalted_value"] = float(ln["exaltedValue"])
            entries.append(entry)
        return entries

    def _parse_rate_converted(self, data: dict[str, Any], category: str) -> list[dict[str, Any]]:
        """PoE2 (both endpoint types) and PoE1's exchange_overview: lines
        carry only `primaryValue`, converted via `core.rates`."""
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

    def _build_entry(
        self,
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
            if unit in rates:
                return primary_value * rates[unit]
            return None

        divine_value = convert("divine")
        exalted_value = convert("exalted")
        chaos_value = convert("chaos")

        if self._game == "poe1":
            # spec 8.1: PoE1 prices are always denominated in chaos.
            value = chaos_value if chaos_value is not None else primary_value
            value_currency = "chaos"
        else:
            value = exalted_value if exalted_value is not None else primary_value
            value_currency = "exalted" if exalted_value is not None else primary_unit

        entry: dict[str, Any] = {
            "category": category,
            "name": name,
            "base": ln.get("baseType") or None,
            "value": value,
            "value_currency": value_currency,
            "divine_value": divine_value,
            "source": "ninja",
            "listing_count": int(ln.get("listingCount") or ln.get("volumePrimaryValue") or 0),
            "trade_id": ln.get("detailsId") or item.get("detailsId") or ln.get("id"),
            "icon_url": ln.get("icon") or item.get("image"),
        }
        # Extra units beyond the required trio, only when the source actually gave them.
        if self._game == "poe1":
            if exalted_value is not None:
                entry["exalted_value"] = exalted_value
        else:
            if chaos_value is not None:
                entry["chaos_value"] = chaos_value
        return entry
