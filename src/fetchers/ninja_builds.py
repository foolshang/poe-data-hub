"""Fetch PoE1/PoE2 character lists per equipped-item-slot from poe.ninja's
builds pages (spec sections 3.3 and 6.1).

There is no public documentation or `.proto` schema for this API. It was
reverse-engineered live on 2026-07-08 (see the pre-2026-09-16 revision of
this file's history for that schema), and **again from scratch on
2026-09-16** after poe.ninja changed the wire format outright -- the
original schema (field1=total, field5=repeated valueList, field6=repeated
dictRef) silently stopped working around 2026-08-04
(`data/samples/last_good_builds_*.bin`'s mtimes are the tell) and every
fetch failed identically for ~43 days before anyone actually re-curled the
live endpoint to check *why* (see CLAUDE.md's "diagnose from the real
endpoint, not the error message's own wording" entry). The failure mode was
subtle: `total` in the response was still a real nonzero population count
the whole time, only the character-row data (the old field 5) was
completely absent -- `_validate_search_response` below now treats exactly
that combination (0 rows extracted, nonzero `total`) as a hard error, never
a silent empty result.

**Current schema** (verified live 2026-09-16, both games, via a real browser
network capture cross-checked byte-for-byte against a from-scratch protobuf
field dump -- see CLAUDE.md for the full diagnostic trail):

Endpoints:

  GET /{game}/api/data/index-state
      -> JSON. `snapshotVersions[]` maps a league display name to the
         {version, snapshotName} pair the search endpoint needs (unchanged
         from the old schema). Also carries `buildLeagues[]` -- **do not**
         use `buildLeagues[].indexed` to decide whether to query a league;
         verified live 2026-09-16 that it does not predict character-data
         availability at all (PoE2's actual main league, "Forbidden Rites",
         reports `indexed: false` yet returns a full 28-column response
         with real character rows, identical in shape to "Runes of Aldur"
         which reports `indexed: true`). `resolve_snapshot` logs this flag
         purely for visibility (cost-free -- `buildLeagues` comes from the
         same response already fetched for `snapshotVersions`, no extra
         request) and must never gate a query on it. Don't re-try this --
         it's already been tried and empirically falsified once.

  GET /{game}/api/builds/{version}/search?overview={snapshotName}&items={value}
      -> protobuf wire format, no known .proto (decoded field-by-field via
         src/utils/pbwire.py). The `items=` filter param **still works
         server-side** -- confirmed live 2026-09-16 by comparing `total`
         and the returned row set with/without it (95895 vs 124352 for the
         same league, different character sets, first name in the list
         differs) -- poe.ninja's own UI just doesn't send it from the
         default "show everyone" view, which is a red herring, not
         evidence it was removed (an earlier pass at this file briefly
         assumed the opposite from that same UI behavior and had to be
         corrected). Per-slot fetching (`fetch_slot`/`_fetch_one_filter`)
         is unchanged in structure from the old schema -- only the response
         *parsing* below is new.

         Response body (field numbers below are the *inner* message found
         at the top-level's sole field 1):
           1: total (varint) -- population count, independent of whether
              any row data is actually present (see the "silent for 43
              days" note above -- never trust an empty result without
              checking this is also 0).
           6: repeated dictRef { 1: name (string), 2: sha1-ish hash (string) }
              -- which /builds/dictionary/{hash} to fetch to resolve a
              dictionary-indexed column (class, gem/skilltrait, ...).
              7 entries for PoE2, 15 for PoE1 (more game systems: bandit,
              atlasskill, mastery, runegraft, tattoo, vestigialmod,
              pantheon, secondascendancy).
           12: repeated valueList (one per column, 28 seen -- up from the
               old schema's 10; poe.ninja added rate/crit/hit-chance/
               projectiles/forks/chains/splits/pierces/aoe/duration). Each
               entry:
                 1, 2: column name (string, both fields carry the same
                    value in every sample seen -- read field 1, fall back
                    to field 2)
                 6: at most one occurrence, a *packed* varint blob holding
                    one value per character in row order -- used for
                    plain-numeric or single-value-per-character
                    dictionary-indexed columns (level, life, energyshield,
                    class)
                 7: repeated string, one occurrence per character -- used
                    for string-valued columns (name, account; also
                    pre-formatted display strings like ehp/dps: "96k",
                    "1.5B")
                 9: repeated, **one occurrence per character** (unlike
                    field 6's single blob) -- each occurrence is itself a
                    length-delimited sub-message whose own field 1 is a
                    packed varint blob; used for multi-value-per-character
                    dictionary-indexed columns (skills, keypassives) since
                    a character can have several
                 11: dictionary name to resolve field 6/9's indices against
                    (string, e.g. "gem") -- self-describing, no need to
                    guess which dictRef applies to which column
         Rows are positionally aligned across columns (row i of "name" is
         the same character as row i of "level", etc), same as before.
         Only the default top-K page is returned (100 rows observed) --
         still no working offset/cursor param.

  GET /{game}/api/builds/dictionary/{hash}
      -> **not protobuf** -- a different, custom binary format (magic bytes
         "NDIC"). See src/utils/ndic.py for the full format writeup and
         decoder. `NdicParseError` (magic/version mismatch) is never
         swallowed -- surfaces as a loud failure the same way a
         `pb.ProtoParseError` does.

Confirmed live 2026-09-16: no bot/browser-fingerprint distinction exists on
either endpoint -- `curl` and a real browser fetch byte-identical responses.
The earlier appearance of a block was entirely mis-parsing the *new* schema
with the *old* field numbers, not an anti-scraping measure. Plain
`requests`-based `HttpClient` calls work fine.

This is materially more fragile than the JSON price endpoints: it's an
internal API with no stability guarantee, and it has already changed
format completely once. `_validate_search_response`, `ndic.decode`, and the
failed-response dump exist specifically so the *next* silent format change
shows up immediately as a loud validation failure with the actual bytes/
fields found (spec 10.1b), not another multi-week silent gap. `_save_last_good*`
keeps the most recent successful response for both the search endpoint
*and* every dictionary name, systematically (not just the search body as
before) -- so diagnosing the next break can start from a real diff instead
of from scratch.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.utils import ndic
from src.utils import pbwire as pb
from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

INDEX_STATE_URL_TEMPLATE = "https://poe.ninja/{game}/api/data/index-state"
SEARCH_URL_TEMPLATE = "https://poe.ninja/{game}/api/builds/{version}/search"
DICTIONARY_URL_TEMPLATE = "https://poe.ninja/{game}/api/builds/dictionary/{hash}"

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")

# Every class/ascendancy name poe.ninja's "class" dictionary is known to
# contain (captured 2026-09-21: 28 PoE1, 31 PoE2 incl. the newly added
# "Druid"). `_get_dictionary` raises on any name outside this set instead of
# carrying it forward -- the 2026-09-21 ndic split bug showed a wrong/garbled
# class dictionary can otherwise pass silently. A genuinely new class means
# updating this set (one line), which is the point: a human looks first.
KNOWN_CLASSES_BY_GAME: dict[str, frozenset[str]] = {
    "poe1": frozenset({
        "Ascendant", "Assassin", "Berserker", "Champion", "Chieftain", "Deadeye", "Duelist", "Elementalist",
        "Gladiator", "Guardian", "Hierophant", "Inquisitor", "Juggernaut", "Luminary", "Marauder", "Necromancer",
        "Occultist", "Pathfinder", "Ranger", "Reliquarian", "Saboteur", "Scion", "Shadow", "Slayer", "Templar",
        "Trickster", "Warden", "Witch",
    }),
    "poe2": frozenset({
        "Abyssal Lich", "Acolyte of Chayula", "Amazon", "Blood Mage", "Chronomancer", "Deadeye",
        "Disciple of Varashta", "Druid", "Gemling Legionnaire", "Huntress", "Infernalist", "Invoker", "Lich",
        "Martial Artist", "Mercenary", "Monk", "Oracle", "Pathfinder", "Ranger", "Ritualist", "Shaman",
        "Smith of Kitava", "Sorceress", "Spirit Walker", "Stormweaver", "Tactician", "Titan", "Warbringer",
        "Warrior", "Witch", "Witchhunter",
    }),
}


class DictionaryValidationError(ValueError):
    """A poe.ninja dictionary decoded but failed a sanity check (unknown
    class name, ...). Never swallowed by `fetch_all`'s per-slot skip -- it
    aborts the whole game's meta run so nothing is published from it."""

# snapshotVersions entries whose `type` is not this are a different
# leaderboard (Delve Depth Solo, streamer-only, ...), not the general
# population sample this project wants -- see module docstring. PoE2
# entries have no `type` key at all; `entry.get("type", _WANTED_SNAPSHOT_TYPE)`
# treats that as an implicit match, same as before this existed.
_WANTED_SNAPSHOT_TYPE = "exp"

# slot -> value(s) passed as the "items" filter (poe.ninja's ITEMS facet).
# Unchanged from the pre-2026-09-16 schema (the filter param still works
# server-side -- see module docstring). PoE2 discovered live 2026-07-08 from
# the "item" dictionary. PoE2 flasks/charms are capped at Magic rarity,
# hence "Magic" not "Rare" for those two. "weapon" and "offhand" have no
# generic bucket in the ITEMS facet -- it's split by base type (Bow, Wand,
# ...) -- and repeated `items=` query params are AND, not OR (verified
# live: items=Rare Bow&items=Rare Wand collapses to ~65 characters wielding
# *both* simultaneously, not either), so each base type needs its own
# fetch; fetch_slot merges and dedupes them by (name, account).
# "jewel" here samples all rare jewels; per-base-type split (jewel:emerald
# etc, spec 6.4) is not available from this endpoint and needs a separate
# approach -- see spec Status.
POE2_SLOT_FILTERS: dict[str, list[str]] = {
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

# PoE1 (phase 10) discovered live 2026-07-14 from the "item" dictionary. No
# "focus"/"charm" slot exists in PoE1 at all -- confirmed absent from the
# item facet entirely, not just omitted here. No "Crossbow"/"Quarterstaff"/
# "Sceptre"/"Spear" either (PoE2-only weapon types); PoE1 instead has
# Claw/Dagger/Sword/Axe types PoE2's own filter list doesn't include.
POE1_SLOT_FILTERS: dict[str, list[str]] = {
    "helmet": ["Rare Helmet"],
    "body": ["Rare Body Armour"],
    "gloves": ["Rare Gloves"],
    "boots": ["Rare Boots"],
    "belt": ["Rare Belt"],
    "ring": ["Rare Ring"],
    "amulet": ["Rare Amulet"],
    "weapon": [
        "Rare Bow", "Rare Claw", "Rare Dagger",
        "Rare One Handed Axe", "Rare One Handed Mace", "Rare One Handed Sword",
        "Rare Two Handed Axe", "Rare Two Handed Mace", "Rare Two Handed Sword",
        "Rare Staff", "Rare Wand",
    ],
    "offhand": ["Rare Shield"],
    "quiver": ["Rare Quiver"],
    "flask": ["Magic Flask"],
    "jewel": ["Rare Jewel"],
}

SLOT_FILTERS_BY_GAME: dict[str, dict[str, list[str]]] = {"poe2": POE2_SLOT_FILTERS, "poe1": POE1_SLOT_FILTERS}


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
    keypassives: tuple[str, ...] = ()


@dataclass
class _ValueColumn:
    """One decoded field-12 valueList entry. Exactly one of `strings`,
    `single_values`, `per_char_values` is populated, matching which wire
    field the column actually used (see module docstring)."""
    label: str
    strings: Optional[list[str]] = None
    single_values: Optional[list[int]] = None
    per_char_values: Optional[list[list[int]]] = None
    dict_name: Optional[str] = None

    def length(self) -> int:
        if self.strings is not None:
            return len(self.strings)
        if self.single_values is not None:
            return len(self.single_values)
        if self.per_char_values is not None:
            return len(self.per_char_values)
        return 0


class NinjaBuildsFetcher:
    def __init__(
        self,
        http_client: HttpClient,
        game: str = "poe2",
        failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR,
        last_good_sample_path: Optional[Path] = None,
    ):
        if game not in SLOT_FILTERS_BY_GAME:
            raise ValueError(f"ninja_builds: unknown game {game!r}")
        self._http = http_client
        self._game = game
        self._slot_filters = SLOT_FILTERS_BY_GAME[game]
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)
        self._last_good_sample_path = last_good_sample_path or Path(f"data/samples/last_good_builds_{game}.bin")
        self._dict_cache: dict[str, list[str]] = {}
        self._search_cache: dict[str, list[CharacterRef]] = {}

    def resolve_snapshot(self, league_name: str) -> SnapshotVersion:
        url = INDEX_STATE_URL_TEMPLATE.format(game=self._game)
        data = self._http.get_json(url)
        self._validate_index_state(data, url)
        for entry in data["snapshotVersions"]:
            if entry.get("name") == league_name and entry.get("type", _WANTED_SNAPSHOT_TYPE) == _WANTED_SNAPSHOT_TYPE:
                self._log_indexed_flag(data, entry)
                return SnapshotVersion(
                    league_url=entry["url"],
                    league_name=entry["name"],
                    version=entry["version"],
                    snapshot_name=entry["snapshotName"],
                )
        raise ValueError(f"ninja_builds({self._game}): no snapshotVersions entry for league {league_name!r}")

    def _log_indexed_flag(self, index_state: dict, snapshot_entry: dict) -> None:
        """Purely informational -- see module docstring for why this must
        never gate a query. Free: `buildLeagues` is part of the same
        response `resolve_snapshot` already fetched."""
        build_leagues = index_state.get("buildLeagues", [])
        match = next((b for b in build_leagues if b.get("url") == snapshot_entry.get("url")), None)
        log.info(
            "ninja_builds(%s): league=%r buildLeagues.indexed=%s (informational only -- confirmed live 2026-09-16 "
            "this does not predict character-data availability, never used as a query gate)",
            self._game, snapshot_entry.get("name"), match.get("indexed") if match else "unknown",
        )

    def _validate_index_state(self, data: Any, url: str) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("snapshotVersions"), list) or not data["snapshotVersions"]:
            self._dump_failed_json(data, url)
            raise ValueError(f"ninja_builds({self._game}): index-state validation failed: {url}")
        self._save_last_good_json(self._last_good_index_state_path(), data)

    def fetch_all(
        self, snapshot: SnapshotVersion, slots: Optional[list[str]] = None
    ) -> tuple[dict[str, list[CharacterRef]], dict[str, int]]:
        """Fetch the character sample for every slot in `slots` (default: all
        slots for this instance's game).

        Returns (per_slot characters, per_slot fetched-count) -- counting
        failures is the caller's job via the second dict having fewer/zero
        entries for a slot that errored (logged, never raised).
        """
        slots = slots if slots is not None else list(self._slot_filters)
        by_slot: dict[str, list[CharacterRef]] = {}
        counts: dict[str, int] = {}
        for slot in slots:
            try:
                chars = self.fetch_slot(snapshot, slot)
                by_slot[slot] = chars
                counts[slot] = len(chars)
            except DictionaryValidationError:
                raise
            except Exception as e:
                counts[slot] = 0
                log.warning("ninja_builds: skip slot %s: %s", slot, e)
        return by_slot, counts

    def fetch_slot(self, snapshot: SnapshotVersion, slot: str) -> list[CharacterRef]:
        item_filters = self._slot_filters.get(slot)
        if item_filters is None:
            raise ValueError(f"ninja_builds({self._game}): unknown slot {slot!r}")

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
        cache_key = f"{snapshot.version}|{item_filter}"
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]

        url = self._build_search_url(snapshot, item_filter)
        raw = self._http.get_bytes(url)

        try:
            body = pb.first(raw, 1)
            if body is None:
                raise pb.ProtoParseError("missing top-level field 1")
            total = pb.first(body, 1)
            columns, dict_refs = self._parse_body(body)
            self._validate_search_response(columns, total, url)
        except pb.ProtoParseError as e:
            self._dump_failed_bytes(raw, url)
            raise ValueError(f"ninja_builds({self._game}): could not parse search response ({url}): {e}") from e

        self._save_last_good(self._last_good_sample_path, raw)
        chars = self._extract_characters(columns, dict_refs, url)
        self._search_cache[cache_key] = chars
        return chars

    def _build_search_url(self, snapshot: SnapshotVersion, item_filter: str) -> str:
        base = SEARCH_URL_TEMPLATE.format(game=self._game, version=snapshot.version)
        overview = urllib.parse.quote(snapshot.snapshot_name)
        items = urllib.parse.quote(item_filter)
        return f"{base}?overview={overview}&items={items}"

    def _parse_body(self, body: bytes) -> tuple[dict[str, "_ValueColumn"], dict[str, str]]:
        columns: dict[str, _ValueColumn] = {}
        for entry in pb.all_of(body, 12):
            col = self._parse_value_column(entry)
            columns[col.label] = col

        dict_refs: dict[str, str] = {}
        for entry in pb.all_of(body, 6):
            name = pb.as_str(pb.first(entry, 1))
            dict_refs[name] = pb.as_str(pb.first(entry, 2))

        return columns, dict_refs

    @staticmethod
    def _parse_value_column(entry: bytes) -> "_ValueColumn":
        label: Optional[str] = None
        strings: list[str] = []
        single_values: list[int] = []
        per_char_values: list[list[int]] = []
        dict_name: Optional[str] = None

        for fn, wt, val in pb.iter_fields(entry):
            if fn in (1, 2) and wt == 2 and label is None:
                label = pb.as_str(val)
            elif fn == 7 and wt == 2:
                strings.append(pb.as_str(val))
            elif fn == 6 and wt == 2:
                single_values.extend(pb.packed_varints(val))
            elif fn == 9 and wt == 2:
                inner = pb.first(val, 1)
                per_char_values.append(pb.packed_varints(inner) if inner else [])
            elif fn == 11 and wt == 2:
                dict_name = pb.as_str(val)

        if label is None:
            raise pb.ProtoParseError("valueList entry missing column name (field 1/2)")

        return _ValueColumn(
            label=label,
            strings=strings or None,
            single_values=single_values or None,
            per_char_values=per_char_values or None,
            dict_name=dict_name,
        )

    def _validate_search_response(self, columns: dict[str, "_ValueColumn"], total: Any, url: str) -> None:
        required = ("name", "account", "level", "class")
        missing = [k for k in required if k not in columns]
        if missing:
            raise pb.ProtoParseError(
                f"missing expected valueList column(s) {missing}; found columns={sorted(columns)} in {url}"
            )

        n = columns["name"].length()
        if n == 0:
            if total:
                # This exact combination -- response `total` nonzero but zero
                # rows actually decoded -- is precisely how the 2026-08-04
                # schema break presented for ~43 days before anyone noticed
                # (see module docstring). Never treat it as a genuine empty
                # result; always a schema mismatch worth failing loudly on.
                raise pb.ProtoParseError(
                    f"parsed 0 characters but response total={total} (nonzero) -- schema mismatch, not a "
                    f"genuine empty result; found columns={sorted(columns)} in {url}"
                )
            log.warning("ninja_builds: 0 characters returned and total=0 (genuinely empty filter result): %s", url)

        mismatched = {name: col.length() for name, col in columns.items() if col.length() not in (0, n)}
        if mismatched:
            raise pb.ProtoParseError(f"valueList column row-count mismatch (expected {n}): {mismatched} in {url}")

    def _extract_characters(
        self, columns: dict[str, "_ValueColumn"], dict_refs: dict[str, str], url: str
    ) -> list[CharacterRef]:
        class_dict = self._get_dictionary("class", dict_refs.get("class"))

        skills_col = columns.get("skills")
        gem_dict: list[str] = []
        if skills_col is not None and skills_col.dict_name:
            gem_dict = self._get_dictionary(skills_col.dict_name, dict_refs.get(skills_col.dict_name))

        keypassives_col = columns.get("keypassives")
        keypassive_dict: list[str] = []
        if keypassives_col is not None and keypassives_col.dict_name:
            keypassive_dict = self._get_dictionary(keypassives_col.dict_name, dict_refs.get(keypassives_col.dict_name))

        names = columns["name"].strings or []
        accounts = columns["account"].strings or []
        levels = columns["level"].single_values or []
        classes = columns["class"].single_values or []

        out: list[CharacterRef] = []
        for i in range(len(names)):
            try:
                name = names[i]
                account = accounts[i]
                level = int(levels[i])
                class_idx = int(classes[i])
                char_class = class_dict[class_idx] if 0 <= class_idx < len(class_dict) else "Unknown"
                main_skill = self._main_skill(skills_col, i, gem_dict)
                keypassives = self._resolve_multi_dict_column(keypassives_col, i, keypassive_dict)
            except (IndexError, ValueError) as e:
                log.warning("ninja_builds: skip malformed row %d: %s", i, e)
                continue
            out.append(CharacterRef(
                name=name, account=account, level=level, char_class=char_class,
                main_skill=main_skill, keypassives=keypassives,
            ))
        return out

    @staticmethod
    def _main_skill(skills_col: Optional["_ValueColumn"], i: int, gem_dict: list[str]) -> Optional[str]:
        """Best-effort: poe.ninja doesn't flag a single "main" skill
        explicitly, so the first gem in the character's per-character skill
        index list is used (matches the ordering poe.ninja's own build page
        displays the primary skill in)."""
        if skills_col is None or not skills_col.per_char_values or not gem_dict:
            return None
        if i >= len(skills_col.per_char_values):
            return None
        indices = skills_col.per_char_values[i]
        if not indices:
            return None
        idx = indices[0]
        return gem_dict[idx] if 0 <= idx < len(gem_dict) else None

    @staticmethod
    def _resolve_multi_dict_column(col: Optional["_ValueColumn"], i: int, value_dict: list[str]) -> tuple[str, ...]:
        """Resolves every index in a per-character multi-value dictionary
        column (e.g. "keypassives") to names, not just the first -- unlike
        `_main_skill`, which only ever wants the primary one. Used for
        skill-meta phase 3 (notable/keystone passives): confirmed live
        2026-09-17 that this "keypassives" column/dictionary is a
        notable/keystone-only enumeration (poe.ninja's own internal ordinal
        index, ~260 entries for PoE2) -- it does NOT cover the full passive
        tree's small stat nodes, and its indices are a completely different
        ID space from the character-detail endpoint's `passiveSelection`
        (real GGG tree node IDs) -- never mix the two up. See CLAUDE.md."""
        if col is None or not col.per_char_values or not value_dict:
            return ()
        if i >= len(col.per_char_values):
            return ()
        return tuple(value_dict[idx] for idx in col.per_char_values[i] if 0 <= idx < len(value_dict))

    def _get_dictionary(self, name: str, hash_: Optional[str]) -> list[str]:
        if not hash_:
            return []
        if hash_ in self._dict_cache:
            return self._dict_cache[hash_]
        url = DICTIONARY_URL_TEMPLATE.format(game=self._game, hash=hash_)
        raw = self._http.get_bytes(url)
        try:
            values = ndic.decode(raw)
        except ndic.NdicParseError as e:
            self._dump_failed_bytes(raw, url)
            raise ValueError(f"ninja_builds({self._game}): could not parse dictionary {name!r} ({url}): {e}") from e
        if name == "class":
            self._validate_class_dictionary(values, url)
        self._save_last_good(self._last_good_dict_path(name), raw)
        self._dict_cache[hash_] = values
        return values

    def _validate_class_dictionary(self, values: list[str], url: str) -> None:
        known = KNOWN_CLASSES_BY_GAME[self._game]
        unknown = sorted(v for v in values if v not in known)
        if unknown:
            raise DictionaryValidationError(
                f"ninja_builds({self._game}): class dictionary has {len(unknown)} unknown name(s) {unknown[:8]!r} "
                f"({len(values)} entries, {url}) -- either poe.ninja added a class (update KNOWN_CLASSES_BY_GAME) "
                f"or the dictionary was mis-decoded; refusing to continue"
            )

    def _last_good_dict_path(self, name: str) -> Path:
        return self._last_good_sample_path.parent / f"last_good_dict_{self._game}_{name}.bin"

    def _last_good_index_state_path(self) -> Path:
        return self._last_good_sample_path.parent / f"last_good_index_state_{self._game}.json"

    def _save_last_good(self, path: Path, raw: bytes) -> None:
        """Keep one raw response from the most recent successful decode
        (overwritten each time, per path -- search response, and now every
        dictionary name too) as a reference to diff against if poe.ninja
        changes the format again -- makes fixing the decoder much faster
        than starting from scratch, the same way `last_good_builds_*.bin`
        already did for this exact incident (see module docstring)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except OSError:
            log.warning("ninja_builds: could not save last-good sample to %s", path, exc_info=True)

    def _save_last_good_json(self, path: Path, data: Any) -> None:
        import json

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("ninja_builds: could not save last-good sample to %s", path, exc_info=True)

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
