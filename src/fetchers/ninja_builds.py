"""Fetch PoE2 character lists per equipped-item-slot from poe.ninja's builds
pages (spec sections 3.3 and 6.1).

There is no public documentation or `.proto` schema for this API — it was
reverse-engineered live on 2026-07-08 by running a headless browser against
https://poe.ninja/poe2/builds/<league> and inspecting network traffic (see
data/samples/ninja_poe2_build_index_state.json, ninja_poe2_index_state.json,
ninja_poe2_builds_search*.json, ninja_poe2_dict_*.json for captured samples).
Endpoints:

  GET /poe2/api/data/index-state
      -> JSON. `snapshotVersions[]` maps a league display name to the
         {version, snapshotName} pair the search endpoint needs.

  GET /poe2/api/builds/{version}/search?overview={snapshotName}&items={value}
      -> a custom binary format (protobuf wire format with no known message
         schema — decoded field-by-field via src/utils/pbwire.py). The `items`
         query param name is plural and filters to characters with that
         value present in poe.ninja's "item" facet (e.g. "Rare Ring"); other
         dimension ids from the response's dictionary-refs section (field 6)
         filter the same way (class=, gem=, weaponmode=, ...).

         Response body (all field numbers below are the *inner* message
         found at the top-level's sole field 1):
           1: total (varint) — count matching the current filters
           5: repeated valueList { 1: name (string), 2: repeated row }
              — one valueList per column; each `row` is itself a tiny
              message: field 1 (string) for text columns, field 2 (varint)
              for numeric/dictionary-index columns, field 3 (packed varint
              bytes, *not* a nested message) for multi-value columns like
              "skills" (a character's equipped skill gems, dictionary-index
              per entry).
           6: repeated dictRef { 1: name (string), 2: sha1-ish hash (string) }
              — which /builds/dictionary/{hash} to fetch to resolve a
              dictionary-indexed valueList (e.g. "class", "gem").
         Rows across all valueLists are positionally aligned (row i of
         "name" is the same character as row i of "level", etc).
         Only the default top-K page (observed: 100 rows, poe.ninja's own
         default sort) is returned — no working offset/cursor param was
         found, so `sample_per_slot` beyond 100 cannot currently be honored.

  GET /poe2/api/builds/dictionary/{hash}
      -> binary. { 1: dict name (string), 2: repeated string } — values are
         indexed by array position (0-based) to resolve a dictionary-index
         row value from the search response.

This is materially more fragile than the JSON price endpoints: it's an
internal API with no stability guarantee. `_validate_search_response` and
the failed-response dump exist specifically so a silent format change shows
up as validation failures + saved raw bytes (spec 10.1b) instead of garbage
character data.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.utils import pbwire as pb
from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

INDEX_STATE_URL = "https://poe.ninja/poe2/api/data/index-state"
SEARCH_URL_TEMPLATE = "https://poe.ninja/poe2/api/builds/{version}/search"
DICTIONARY_URL_TEMPLATE = "https://poe.ninja/poe2/api/builds/dictionary/{hash}"

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")
LAST_GOOD_SAMPLE_PATH = Path("data/samples/last_good_builds.bin")

# slot -> value(s) passed as the "items" filter (poe.ninja's ITEMS facet).
# Discovered live 2026-07-08 from the "item" dictionary
# (data/samples/ninja_poe2_dict_6cf3eaac2a9cfcf9dd7d89a9063407550338c3a2.json).
# PoE2 flasks/charms are capped at Magic rarity, hence "Magic" not "Rare" for those two.
# "weapon" and "offhand" have no generic bucket in the ITEMS facet -- it's split by base
# type (Bow, Wand, ...) -- and repeated `items=` query params are AND, not OR (verified
# live: items=Rare Bow&items=Rare Wand collapses to ~65 characters wielding *both*
# simultaneously, not either), so each base type needs its own fetch; fetch_slot merges
# and dedupes them by (name, account).
# "jewel" here samples all rare jewels; per-base-type split (jewel:emerald etc, spec 6.4)
# is not available from this endpoint and needs a separate approach -- see spec Status.
SLOT_FILTERS: dict[str, list[str]] = {
    "helmet": ["Rare Helmet"],
    "body": ["Rare Body Armour"],
    "gloves": ["Rare Gloves"],
    "boots": ["Rare Boots"],
    "belt": ["Rare Belt"],
    "ring": ["Rare Ring"],
    "amulet": ["Rare Amulet"],
    "weapon": [
        "Rare Bow", "Rare Crossbow", "Rare One Handed Mace", "Rare Two Handed Mace",
        "Rare Quarterstaff", "Rare Sceptre", "Rare Spear", "Rare Staff", "Rare Wand",
    ],
    "offhand": ["Rare Buckler", "Rare Shield"],
    "quiver": ["Rare Quiver"],
    "focus": ["Rare Focus"],
    "flask": ["Magic Flask"],
    "charm": ["Magic Charm"],
    "jewel": ["Rare Jewel"],
}


@dataclass(frozen=True)
class SnapshotVersion:
    league_url: str
    league_name: str
    version: str
    snapshot_name: str


@dataclass(frozen=True)
class CharacterRef:
    name: str
    account: str
    level: int
    char_class: str
    main_skill: Optional[str]


class NinjaBuildsFetcher:
    def __init__(
        self,
        http_client: HttpClient,
        failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR,
        last_good_sample_path: Path = LAST_GOOD_SAMPLE_PATH,
    ):
        self._http = http_client
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)
        self._last_good_sample_path = last_good_sample_path
        self._dict_cache: dict[str, list[str]] = {}

    def resolve_snapshot(self, league_name: str) -> SnapshotVersion:
        data = self._http.get_json(INDEX_STATE_URL)
        self._validate_index_state(data)
        for entry in data["snapshotVersions"]:
            if entry.get("name") == league_name:
                return SnapshotVersion(
                    league_url=entry["url"],
                    league_name=entry["name"],
                    version=entry["version"],
                    snapshot_name=entry["snapshotName"],
                )
        raise ValueError(f"ninja_builds: no snapshotVersions entry for league {league_name!r}")

    def _validate_index_state(self, data: Any) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("snapshotVersions"), list) or not data["snapshotVersions"]:
            self._dump_failed_json(data, INDEX_STATE_URL)
            raise ValueError(f"ninja_builds: index-state validation failed: {INDEX_STATE_URL}")

    def fetch_all(
        self, snapshot: SnapshotVersion, slots: Optional[list[str]] = None
    ) -> tuple[dict[str, list[CharacterRef]], dict[str, int]]:
        """Fetch the character sample for every slot in `slots` (default: all of SLOT_FILTERS).

        Returns (per_slot characters, per_slot fetched-count) — counting
        failures is the caller's job via the second dict having fewer/zero
        entries for a slot that errored (logged, never raised).
        """
        slots = slots if slots is not None else list(SLOT_FILTERS)
        by_slot: dict[str, list[CharacterRef]] = {}
        counts: dict[str, int] = {}
        for slot in slots:
            try:
                chars = self.fetch_slot(snapshot, slot)
                by_slot[slot] = chars
                counts[slot] = len(chars)
            except Exception as e:
                counts[slot] = 0
                log.warning("ninja_builds: skip slot %s: %s", slot, e)
        return by_slot, counts

    def fetch_slot(self, snapshot: SnapshotVersion, slot: str) -> list[CharacterRef]:
        item_filters = SLOT_FILTERS.get(slot)
        if item_filters is None:
            raise ValueError(f"ninja_builds: unknown slot {slot!r}")

        seen: set[tuple[str, str]] = set()
        merged: list[CharacterRef] = []
        for item_filter in item_filters:
            for char in self._fetch_one_filter(snapshot, item_filter):
                key = (char.name, char.account)
                if key not in seen:
                    seen.add(key)
                    merged.append(char)
        return merged

    def _fetch_one_filter(self, snapshot: SnapshotVersion, item_filter: str) -> list[CharacterRef]:
        url = self._build_search_url(snapshot, item_filter)
        raw = self._http.get_bytes(url)

        try:
            body = pb.first(raw, 1)
            if body is None:
                raise pb.ProtoParseError("missing top-level field 1")
            value_lists, dict_refs = self._parse_body(body)
            self._validate_search_response(value_lists, url)
        except pb.ProtoParseError as e:
            self._dump_failed_bytes(raw, url)
            raise ValueError(f"ninja_builds: could not parse search response ({url}): {e}") from e

        self._save_last_good_sample(raw)
        return self._extract_characters(value_lists, dict_refs)

    def _save_last_good_sample(self, raw: bytes) -> None:
        """Keep one raw response from the most recent successful decode
        (overwritten each time) as a reference to diff against if poe.ninja
        changes this undocumented binary format -- makes fixing the decoder
        much faster than starting from scratch."""
        try:
            self._last_good_sample_path.parent.mkdir(parents=True, exist_ok=True)
            self._last_good_sample_path.write_bytes(raw)
        except OSError:
            log.warning("ninja_builds: could not save last-good sample", exc_info=True)

    def _build_search_url(self, snapshot: SnapshotVersion, item_filter: str) -> str:
        base = SEARCH_URL_TEMPLATE.format(version=snapshot.version)
        overview = urllib.parse.quote(snapshot.snapshot_name)
        items = urllib.parse.quote(item_filter)
        return f"{base}?overview={overview}&items={items}"

    def _parse_body(self, body: bytes) -> tuple[dict[str, list[bytes]], dict[str, str]]:
        value_lists: dict[str, list[bytes]] = {}
        for entry in pb.all_of(body, 5):
            name = pb.as_str(pb.first(entry, 1))
            value_lists[name] = pb.all_of(entry, 2)

        dict_refs: dict[str, str] = {}
        for entry in pb.all_of(body, 6):
            name = pb.as_str(pb.first(entry, 1))
            dict_refs[name] = pb.as_str(pb.first(entry, 2))

        return value_lists, dict_refs

    def _validate_search_response(self, value_lists: dict[str, list[bytes]], url: str) -> None:
        required = ("name", "account", "level", "class")
        missing = [k for k in required if k not in value_lists]
        if missing:
            raise pb.ProtoParseError(f"missing expected valueList(s) {missing} in {url}")
        n = len(value_lists["name"])
        if n == 0:
            log.warning("ninja_builds: 0 characters returned (possibly an empty filter result): %s", url)
        mismatched = {k: len(v) for k, v in value_lists.items() if len(v) != n}
        if mismatched:
            raise pb.ProtoParseError(f"valueList row-count mismatch (expected {n}): {mismatched}")

    def _extract_characters(
        self, value_lists: dict[str, list[bytes]], dict_refs: dict[str, str]
    ) -> list[CharacterRef]:
        class_dict = self._get_dictionary("class", dict_refs.get("class"))
        gem_dict = self._get_dictionary("gem", dict_refs.get("gem")) if "skills" in value_lists else []

        names = value_lists["name"]
        out: list[CharacterRef] = []
        for i in range(len(names)):
            try:
                name = self._row_str(value_lists["name"][i])
                account = self._row_str(value_lists["account"][i])
                level = self._row_int(value_lists["level"][i])
                class_idx = self._row_int(value_lists["class"][i])
                char_class = class_dict[class_idx] if 0 <= class_idx < len(class_dict) else "Unknown"
                main_skill = self._main_skill(value_lists.get("skills"), i, gem_dict)
            except Exception as e:
                log.warning("ninja_builds: skip malformed row %d: %s", i, e)
                continue
            out.append(CharacterRef(name=name, account=account, level=level, char_class=char_class, main_skill=main_skill))
        return out

    @staticmethod
    def _row_str(row: bytes) -> str:
        v = pb.first(row, 1)
        return pb.as_str(v) if v is not None else ""

    @staticmethod
    def _row_int(row: bytes) -> int:
        v = pb.first(row, 2)
        return int(v) if v is not None else 0

    @staticmethod
    def _main_skill(skill_rows: Optional[list[bytes]], i: int, gem_dict: list[str]) -> Optional[str]:
        """Best-effort: poe.ninja doesn't flag a single "main" skill explicitly,
        so the first gem in the character's packed skill-index list is used
        (matches the ordering PoB/poe.ninja display for the primary skill)."""
        if not skill_rows or not gem_dict:
            return None
        packed = pb.first(skill_rows[i], 3)
        if not packed:
            return None
        indices = pb.packed_varints(packed)
        if not indices:
            return None
        idx = indices[0]
        return gem_dict[idx] if 0 <= idx < len(gem_dict) else None

    def _get_dictionary(self, name: str, hash_: Optional[str]) -> list[str]:
        if not hash_:
            return []
        if hash_ in self._dict_cache:
            return self._dict_cache[hash_]
        url = DICTIONARY_URL_TEMPLATE.format(hash=hash_)
        raw = self._http.get_bytes(url)
        try:
            values = [pb.as_str(v) for v in pb.all_of(raw, 2)]
        except pb.ProtoParseError as e:
            self._dump_failed_bytes(raw, url)
            raise ValueError(f"ninja_builds: could not parse dictionary {name!r} ({url}): {e}") from e
        self._dict_cache[hash_] = values
        return values

    def _dump_failed_bytes(self, raw: bytes, url: str) -> None:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        path = self._failed_dir / f"{host}_{int(time.time())}.bin"
        try:
            path.write_bytes(raw)
        except OSError:
            log.warning("ninja_builds: could not write failed response dump", exc_info=True)

    def _dump_failed_json(self, data: Any, url: str) -> None:
        import json

        host = url.split("//", 1)[-1].split("/", 1)[0]
        path = self._failed_dir / f"{host}_{int(time.time())}.json"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("ninja_builds: could not write failed response dump", exc_info=True)
