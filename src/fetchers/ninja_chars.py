"""Fetch full character detail (gear, mods, jewels, main skill) from poe.ninja
(spec sections 3.4 and 6.2).

Unlike the builds-search endpoint (src/fetchers/ninja_builds.py), this one is
plain JSON — reverse-engineered live on 2026-07-08 the same way, by loading
https://poe.ninja/poe2/builds/<league>/character/<account>/<name> in a
headless browser and inspecting network traffic (see
data/samples/ninja_poe2_char_detail.json for the captured sample):

  GET /{game}/api/builds/{version}/character
      ?account={account}&name={name}&overview={snapshotName}&timeMachine=

PoE1 (phase 10) uses the exact same shape under `/poe1/` -- confirmed live
2026-07-14 (data/samples/ninja_poe1_char_detail.json): same top-level keys,
same `itemData`/`inventoryId`/mod-array structure, same `PassiveJewels`
inventoryId for jewels. No code branching needed beyond the URL's game
segment and `SLOT_BY_INVENTORY_ID` already covering every inventoryId seen
in a real PoE1 character (Amulet/Belt/BodyArmour/Boots/Gloves/Helm/Offhand/
Offhand2/Ring/Ring2/Weapon/Weapon2 -- no "Focus", PoE1 has no focus slot,
consistent with it never appearing there).

Mod text comes from `itemData.explicitMods` / `.implicitMods` / `.fracturedMods`
— GGG-style display strings with reference-tag brackets, e.g.
`"+24% to all [ElementalDamage|Elemental] [Resistances]"`. These are cleaned
(bracket tags collapsed to their display portion) before being handed to
src/processing/normalizer.py, which expects plain text matching the trade(2)
stats table's templates — NOT poe.ninja's own internal `mods.explicit[].stats`
keys, which use a different, non-trade-API vocabulary.

`inventoryId` -> our slot vocabulary (spec 8.2) is a fixed mapping
(SLOT_BY_INVENTORY_ID below); jewels use `PassiveJewels` for every base type
and are split by `baseType` per spec 6.4 (jewel:emerald, jewel:diamond, ...).
`Chakra` (PoE2 rune-socket items) has no slot in spec 8.2 and is skipped.

PoE1's jewel `baseType` strings carry a trailing "Jewel" word PoE2's don't
("Cobalt Jewel" vs PoE2's bare "Diamond"/"Emerald"), and cluster jewels carry
a size prefix ("Medium Cluster Jewel", "Large Cluster Jewel", ...) -- both
stripped in `_slugify_jewel_base` (phase 10) so PoE1 produces the same clean
`jewel:cobalt`/`jewel:cluster`/`jewel:abyss` shape as PoE2's `jewel:diamond`,
confirmed live 2026-07-14 against real PoE1 character data (Cobalt/Viridian/
Prismatic jewels + Medium/Large Cluster Jewels all seen in one character).
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.fetchers.ninja_builds import SnapshotVersion
from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

CHARACTER_URL_TEMPLATE = "https://poe.ninja/{game}/api/builds/{version}/character"

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")
KNOWN_GAMES = {"poe1", "poe2"}

SLOT_BY_INVENTORY_ID: dict[str, str] = {
    "Weapon": "weapon",
    "Weapon2": "weapon",
    "Offhand": "offhand",
    "Offhand2": "offhand",
    "Helm": "helmet",
    "BodyArmour": "body",
    "Gloves": "gloves",
    "Boots": "boots",
    "Belt": "belt",
    "Ring": "ring",
    "Ring2": "ring",
    "Amulet": "amulet",
    "Quiver": "quiver",
    "Focus": "focus",
}

# Reference-tag cleanup: "[Foo|Bar]" -> "Bar" (display text after the pipe),
# "[Foo]" -> "Foo" (no pipe = the bracket content IS the display text).
_REF_TAG_RE = re.compile(r"\[([^\[\]|]+)(?:\|([^\[\]]+))?\]")

# PoE1-only: "Small/Medium/Large Cluster Jewel" -> "Cluster Jewel" first, then
# the trailing "Jewel" word strip below collapses it to just "Cluster".
_CLUSTER_SIZE_PREFIX_RE = re.compile(r"^(?:Small|Medium|Large)\s+", re.IGNORECASE)
_TRAILING_JEWEL_WORD_RE = re.compile(r"\bJewel\b", re.IGNORECASE)


def _clean_mod_text(raw: str) -> str:
    return _REF_TAG_RE.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), raw)


def _slugify_jewel_base(base_type: str) -> str:
    name = _CLUSTER_SIZE_PREFIX_RE.sub("", base_type.strip())
    name = _TRAILING_JEWEL_WORD_RE.sub("", name).strip()
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    return "jewel:" + (slug or "unknown")


@dataclass(frozen=True)
class ItemMods:
    implicit: list[str] = field(default_factory=list)
    explicit: list[str] = field(default_factory=list)
    fractured: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ItemDetail:
    slot: str
    base_type: str
    name: str
    frame_type_id: str
    mods: ItemMods


@dataclass(frozen=True)
class CharacterDetail:
    account: str
    name: str
    level: int
    char_class: str
    main_skill: Optional[str]
    items: list[ItemDetail]
    # Raw GGG passive-tree node IDs (real tree numbering, NOT poe.ninja's own
    # small ordinal dictionaries like the builds-search endpoint's
    # "keypassives" column -- see ninja_builds.py's module docstring/
    # CLAUDE.md for why those two are incompatible ID spaces). Stored
    # verbatim in char_items (skill-meta phase 3, path A) at near-zero
    # marginal cost, so a future "path B" (full passive tree via real GGG
    # tree data) has historical data to work from instead of starting the
    # clock over. Not resolved to names anywhere yet -- no tree dataset is
    # wired in.
    passive_selection: tuple[int, ...] = ()


def item_detail_from_dict(d: dict[str, Any]) -> ItemDetail:
    """Reconstructs an `ItemDetail` from `dataclasses.asdict(item)`'s shape
    (what `char_items.items_json` actually stores) -- used by
    `--reprocess-char-items` (skill-meta phase 2, item 13) to recompute
    skill-dimension mod counts from already-stored raw data without
    re-fetching from poe.ninja."""
    mods = d.get("mods") or {}
    return ItemDetail(
        slot=d["slot"],
        base_type=d.get("base_type", ""),
        name=d.get("name", ""),
        frame_type_id=d.get("frame_type_id", ""),
        mods=ItemMods(
            implicit=list(mods.get("implicit", [])),
            explicit=list(mods.get("explicit", [])),
            fractured=list(mods.get("fractured", [])),
        ),
    )


class NinjaCharsFetcher:
    def __init__(self, http_client: HttpClient, game: str = "poe2", failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR):
        if game not in KNOWN_GAMES:
            raise ValueError(f"ninja_chars: unknown game {game!r}")
        self._http = http_client
        self._game = game
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)

    def fetch_character(self, snapshot: SnapshotVersion, account: str, name: str) -> CharacterDetail:
        url = self._build_url(snapshot, account, name)
        data = self._http.get_json(url)
        self._validate(data, url)
        return self._parse(data)

    def _build_url(self, snapshot: SnapshotVersion, account: str, name: str) -> str:
        params = {
            "account": account,
            "name": name,
            "overview": snapshot.snapshot_name,
            "timeMachine": "",
        }
        return f"{CHARACTER_URL_TEMPLATE.format(game=self._game, version=snapshot.version)}?{urllib.parse.urlencode(params)}"

    def _validate(self, data: Any, url: str) -> None:
        required = ("account", "name", "level", "class", "items")
        if not isinstance(data, dict) or any(k not in data for k in required):
            self._dump_failed_response(data, url)
            raise ValueError(f"ninja_chars: response validation failed for {url}")
        if not isinstance(data["items"], list):
            self._dump_failed_response(data, url)
            raise ValueError(f"ninja_chars: 'items' is not a list ({url})")

    def _parse(self, data: dict[str, Any]) -> CharacterDetail:
        items: list[ItemDetail] = []
        for raw_item in [*data.get("items", []), *data.get("jewels", []), *data.get("flasks", [])]:
            item = self._parse_item(raw_item)
            if item is not None:
                items.append(item)

        main_skill = self._extract_main_skill(data.get("skills"))
        passive_selection = tuple(int(n) for n in data.get("passiveSelection") or [])

        return CharacterDetail(
            account=data["account"],
            name=data["name"],
            level=int(data["level"]),
            char_class=data["class"],
            main_skill=main_skill,
            items=items,
            passive_selection=passive_selection,
        )

    def _parse_item(self, raw_item: dict[str, Any]) -> Optional[ItemDetail]:
        idata = raw_item.get("itemData", raw_item)
        inventory_id = idata.get("inventoryId", "")

        if inventory_id == "PassiveJewels":
            slot = _slugify_jewel_base(idata.get("baseType", "unknown"))
        elif inventory_id == "Flask":
            base = (idata.get("baseType") or "").lower()
            slot = "charm" if "charm" in base else "flask"
        else:
            slot = SLOT_BY_INVENTORY_ID.get(inventory_id)
            if slot is None:
                return None  # e.g. "Chakra" rune-socket items — not a spec 8.2 slot

        mods = ItemMods(
            implicit=[_clean_mod_text(m) for m in idata.get("implicitMods", []) or []],
            explicit=[_clean_mod_text(m) for m in idata.get("explicitMods", []) or []],
            fractured=[_clean_mod_text(m) for m in idata.get("fracturedMods", []) or []],
        )
        return ItemDetail(
            slot=slot,
            base_type=idata.get("baseType", ""),
            name=idata.get("name", ""),
            frame_type_id=idata.get("frameTypeId", ""),
            mods=mods,
        )

    @staticmethod
    def _extract_main_skill(skills: Any) -> Optional[str]:
        if not isinstance(skills, list) or not skills:
            return None
        try:
            return skills[0]["allGems"][0]["name"]
        except (KeyError, IndexError, TypeError):
            return None

    def _dump_failed_response(self, data: Any, url: str) -> None:
        import json

        host = url.split("//", 1)[-1].split("/", 1)[0]
        path = self._failed_dir / f"{host}_{int(time.time())}.json"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("ninja_chars: could not write failed response dump", exc_info=True)


class DedupedCharacterCache:
    """Ensures each (account, name) is fetched at most once per snapshot run
    (spec 6.2) even though the same character can appear in multiple
    per-slot lists from ninja_builds.NinjaBuildsFetcher.fetch_all."""

    def __init__(self, fetcher: NinjaCharsFetcher, snapshot: SnapshotVersion):
        self._fetcher = fetcher
        self._snapshot = snapshot
        self._cache: dict[tuple[str, str], Optional[CharacterDetail]] = {}
        self.fetched_count = 0
        self.cache_hit_count = 0
        self.failed_count = 0

    def get(self, account: str, name: str) -> Optional[CharacterDetail]:
        key = (account, name)
        if key in self._cache:
            self.cache_hit_count += 1
            return self._cache[key]

        try:
            detail = self._fetcher.fetch_character(self._snapshot, account, name)
            self.fetched_count += 1
        except Exception as e:
            log.warning("ninja_chars: skip character %s/%s: %s", account, name, e)
            detail = None
            self.failed_count += 1

        self._cache[key] = detail
        return detail
