"""Item-text -> GGG stat aliases generated from RePoE's stat translations.

Why this exists. GGG's trade stats table prints ONE example wording per stat ("Has 1 Socket",
"#% reduced Elemental Ailment Duration on you", "Gain # Charge when ..."), but the game renders
the same stat with several wordings depending on its value: singular/plural, a sign flip
("reduced" <-> "increased"), a text without the number ("Chance to Block is Lucky"), ... Guessing
those from the text produced wrong rules more than once (see CLAUDE.md, 2026-10-19). RePoE (a
community datamine of the game's own stat translations) lists every wording of every stat together
with the value condition and index handlers that select it AND the GGG trade stat ids, so which
wording belongs to which stat can be READ rather than inferred. One generator, one pass, reads:

  * value conditions     -> singular/plural ([1..1] / [2..]) and "N% chance to X" vs "X" ([1..99] / [100..])
  * `negate`             -> mirrored wordings (increased/reduced, faster/slower), stored sign follows the table wording
  * unit handlers        -> divide_by_one_hundred, milliseconds_to_seconds: item numbers are DISPLAYED units
  * placeholder-less text-> implied value, but ONLY in the three provable shapes (see _implied_stored_values)

Anything it cannot prove is left out and counted (`stats` in the payload): other handlers, negated
conditions, multi-stat strings that do not show every stat, an unconditional [None..None] wording
(the "default branch" of an ordered variant list: the real value is unknown), one-sided bounds with no
complementary sibling.

Stored values follow the convention of the existing exact-match path: the number printed on the
item, with the sign expressed in terms of the wording the GGG table lists for that stat.

Pure functions only (no network): tools/refresh_rendering_aliases.py fetches and publishes, and
ModNormalizer loads the published payload as its last-resort rule.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Optional

SCHEMA_VERSION = 1
KINDS = ("explicit", "implicit", "fractured")

_PLACE = re.compile(r"\{(\d*)(?::[^}]*)?\}")
_NUM = r"[+-]?\d+(?:\.\d+)?"
_MARKUP = re.compile(r"\[([^\]|]*\|)?([^\]]*)\]")          # PoE2 "[Keyword|shown]" -> shown
_TRADE_ID = re.compile(r"^([a-z]+)\.stat_(\d+)$")
_DEPRECATED_ID = re.compile(r"^old_|_old_")

# handler name(s) for one stat -> (negate?, divisor: displayed = internal / divisor)
_HANDLERS: dict[tuple[str, ...], tuple[bool, int]] = {
    (): (False, 1),
    ("negate",): (True, 1),
    ("divide_by_one_hundred",): (False, 100),
    ("milliseconds_to_seconds",): (False, 1000),
    ("milliseconds_to_seconds_2dp_if_required",): (False, 1000),
}


def _clean(s: str) -> str:
    return _MARKUP.sub(lambda m: m.group(2), s)


def _norm(s: str) -> str:
    """wording with every placeholder/number-sign collapsed, to compare with a GGG table text"""
    return re.sub(r"[+-]?#", "#", _PLACE.sub("#", s)).strip()


def _cond(c: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    return c.get("min"), c.get("max")


def _implied_stored_values(cond: list[dict[str, Any]], siblings: list[list[dict[str, Any]]], divisor: int, table_negate: bool) -> Optional[list[float]]:
    """Stored value(s) for a wording WITHOUT a placeholder, or None when the value is not provable.

    Only three shapes are accepted (everything else, including [None..None], stays unmapped):
      1. exactly one value:            [v..v]                      -> v
      2. open above, with a sibling that closes the range just below it: [lo..] + [..lo-1]
                                       ("X" is the 100% case of "N% chance to X") -> lo
      3. sign only, with the mirrored sign as a sibling: [1..] + [..-1] (Lucky / Unlucky) -> []  (nothing
         is printed on the item, so, like the exact path, no value is stored)
    """
    if len(cond) != 1:
        return None
    lo, hi = _cond(cond[0])
    if lo is not None and hi is not None and lo == hi:
        internal = float(lo)
    elif lo is not None and hi is None:
        sib = [_cond(c[0]) for c in siblings if len(c) == 1]
        if lo == 1 and any(s[0] is None and s[1] == -1 for s in sib):
            return []                                               # shape 3
        if any(s[1] == lo - 1 for s in sib if s[1] is not None):
            internal = float(lo)                                    # shape 2
        else:
            return None
    elif lo is None and hi is not None:
        sib = [_cond(c[0]) for c in siblings if len(c) == 1]
        if hi == -1 and any(s[0] == 1 and s[1] is None for s in sib):
            return []                                               # shape 3 (the mirrored side)
        return None
    else:
        return None                                                 # [None..None]: default branch, value unknown
    stored = internal / divisor
    if table_negate:
        stored = -stored
    return [round(stored, 6)]


def build_aliases(game: str, translations: list[dict[str, Any]], table: dict[str, dict[str, str]]) -> dict[str, Any]:
    """`translations`: RePoE stat_translations entries. `table`: {kind: {GGG stat id: table text}}.
    Returns the publishable payload (see module docstring)."""
    stats: dict[str, int] = {}

    def bump(k: str) -> None:
        stats[k] = stats.get(k, 0) + 1

    deprecated: set[str] = set()
    current: set[str] = set()                  # trade ids carried by at least one non-deprecated entry
    drafts: list[dict[str, Any]] = []

    for entry in translations:
        trade = entry.get("trade_stats") or []
        nums = sorted({m.group(2) for t in trade if (m := _TRADE_ID.match(t.get("id", "")))})
        if not nums:
            continue
        bump("entries_with_trade_id")
        if len(nums) != 1:
            bump("skipped_entry_multiple_trade_ids")
            continue
        num = nums[0]
        kinds = [k for k in KINDS if f"{k}.stat_{num}" in table.get(k, {})]
        if not kinds:
            bump("skipped_entry_id_not_in_ggg_table")
            continue
        ids = entry["ids"]
        is_dep = any(_DEPRECATED_ID.search(i) for i in ids)
        (deprecated if is_dep else current).add(num)

        variants = entry.get("English") or []
        parsed = []                                # per variant: dict or None (unsupported)
        for v in variants:
            handlers = [tuple(h) for h in v.get("index_handlers", [])]
            resolved = [_HANDLERS.get(h) for h in handlers]
            conds = v.get("condition", [])
            if any(r is None for r in resolved) or any(c.get("negated") for c in conds):
                bump("unsupported_handler_or_negated_condition")
                parsed.append(None)
                continue
            s = _clean(v["string"])
            order = [int(m.group(1) or 0) for m in _PLACE.finditer(s)]
            parsed.append({"s": s, "order": order, "negate": [r[0] for r in resolved], "div": [r[1] for r in resolved], "cond": conds})

        # the table's own wording of this stat decides the sign convention of the stored values
        table_text = _norm(table[kinds[0]][f"{kinds[0]}.stat_{num}"])
        table_negate: list[bool] = [False] * len(ids)
        for pv in parsed:
            if pv and _norm(pv["s"]) == table_text and pv["negate"]:
                table_negate = list(pv["negate"]) + [False] * (len(ids) - len(pv["negate"]))
                break

        # unit divisor for implied values: taken from the placeholder wordings of the same entry, which must agree
        entry_unsupported = any(p is None for p in parsed)
        divisors: Optional[list[int]] = None
        consistent = True
        for pv in parsed:
            if pv and pv["order"]:
                d = pv["div"]
                if divisors is None:
                    divisors = d
                elif divisors != d:
                    consistent = False
        sibling_conds = [pv["cond"] for pv in parsed if pv]

        for pv in parsed:
            if pv is None:
                continue
            bump("variants_seen")
            order = pv["order"]
            if order:
                if len(set(order)) != len(order) or len(set(order)) != len(ids) or len(pv["cond"]) != len(ids) or len(pv["negate"]) != len(ids):
                    bump("skipped_variant_multi_stat_or_repeated_placeholder")
                    continue
                stats_idx = order
                alias = {
                    "x": pv["s"], "n": num, "k": kinds, "p": stats_idx,
                    "ng": pv["negate"], "dv": pv["div"],
                    "sg": [-1 if (pv["negate"][i] != table_negate[i]) else 1 for i in range(len(ids))],
                    "c": [[c.get("min"), c.get("max")] for c in pv["cond"]],
                    "iv": None, "dep": is_dep,
                }
            else:
                if len(ids) != 1 or entry_unsupported or not consistent:
                    bump("skipped_implied_value_entry_not_provable")
                    continue
                divisor = divisors[0] if divisors else 1
                iv = _implied_stored_values(pv["cond"], [c for c in sibling_conds if c is not pv["cond"]], divisor, table_negate[0])
                if iv is None:
                    bump("skipped_implied_value_not_provable")
                    continue
                alias = {"x": pv["s"], "n": num, "k": kinds, "p": [], "ng": [], "dv": [], "sg": [], "c": [], "iv": iv, "dep": is_dep}
            drafts.append(alias)

    # GGG gives one trade id to several internal stats: an id that a current stat also carries is not deprecated
    # (checked 2026-10-19: 24 of the 28 ids behind old_* stats are shared this way and nothing is wrong with them)
    deprecated -= current

    # drop wordings the exact-match path already handles, unless a DEPRECATED stat renders the same text
    # (then the current stat's alias is needed so it can be preferred over the deprecated table match)
    nums_by_text: dict[str, set[str]] = {}
    for a in drafts:
        nums_by_text.setdefault(_norm(a["x"]), set()).add(a["n"])
    aliases = []
    for a in drafts:
        text = _norm(a["x"])
        a["dep"] = a["n"] in deprecated
        redundant = all(_norm(table[k][f"{k}.stat_{a['n']}"]) == text for k in a["k"])
        shares_with_deprecated = len(nums_by_text[text]) > 1 and any(n in deprecated for n in nums_by_text[text])
        if redundant and not shares_with_deprecated:
            stats["dropped_redundant_with_table_text"] = stats.get("dropped_redundant_with_table_text", 0) + 1
            continue
        aliases.append(a)

    return {
        "schema_version": SCHEMA_VERSION,
        "game": game,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": dict(sorted(stats.items())),
        "deprecated": sorted(deprecated),
        "aliases": aliases,
    }


PUBLIC_SCHEMA_VERSION = 1


def build_stat_groups(table: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Stat ids of one kind that render the same text, which the game itself cannot tell apart from the item.

    The GGG table gives several stat ids the SAME text (PoE1: 262 texts, CLAUDE.md 2026-10-19) or texts that
    differ only in letter case ("supported" / "Supported"). A consumer that matches by text (ModNormalizer,
    the PoE Checker) lands on one member; this lists every member so the others can still be found.
      * reason "identical_text": every member has exactly the same table text. `canonical` is the id the hub
        emits for it (the last one in table order, ModNormalizer's rule); the others are sibling aliases.
      * reason "case_only": the texts differ only in case. The hub emits the member whose wording matches the
        item's own casing, so rows may exist under ANY member: `canonical` is null, look all members up.
    Groups never span kinds (explicit.stat_1 and implicit.stat_1 are different rows)."""
    groups: list[dict[str, Any]] = []
    for kind in KINDS:
        by_fold: dict[str, list[tuple[str, str]]] = {}
        for stat_id, text in table.get(kind, {}).items():
            by_fold.setdefault(text.casefold(), []).append((stat_id, text))
        for members in by_fold.values():
            if len(members) < 2:
                continue
            ids = [m[0] for m in members]
            identical = len({m[1] for m in members}) == 1
            groups.append({
                "kind": kind,
                "reason": "identical_text" if identical else "case_only",
                "canonical": ids[-1] if identical else None,
                "members": sorted(ids),
                "text": members[-1][1],
            })
    groups.sort(key=lambda g: (g["kind"], g["members"][0]))
    return groups


# internal (hub-private, compact) alias field -> public (readable) alias field. The internal names are what the
# running hub reads from internal/rendering_aliases_{game}.json and must not change without a coordinated deploy;
# the public file is a separate artefact so its contract can be readable.
_PUBLIC_FIELDS = {
    "x": "text", "n": "trade_id", "k": "kinds", "p": "placeholder_stat_index", "ng": "negate", "dv": "divisor",
    "sg": "sign", "c": "conditions", "iv": "implied_values", "dep": "deprecated",
}


def to_public(payload: dict[str, Any], groups: list[dict[str, Any]], repoe_url: str) -> dict[str, Any]:
    """The `{game}/aliases/latest.json` contract (SPEC.md 8.12) from an internal payload + stat groups."""
    aliases = [{_PUBLIC_FIELDS[k]: a[k] for k in _PUBLIC_FIELDS} for a in payload["aliases"]]
    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "generated_at": payload["generated_at"],
        "game": payload["game"],
        "source": {
            "stat_translations": repoe_url,
            "note": "RePoE is a community datamine and lags the game by days after a patch (PoE2 2-7 days, PoE1 ~0.4 day, "
                    "measured 2026-10): early in a league some wordings are missing. Missing is not broken.",
        },
        "deprecated_trade_ids": payload["deprecated"],
        "aliases": aliases,
        "stat_groups": groups,
        "stat_aliases": {m: g["canonical"] for g in groups if g["canonical"] for m in g["members"] if m != g["canonical"]},
    }


def from_public_alias(a: dict[str, Any]) -> dict[str, Any]:
    """Inverse of the field renaming in `to_public` (used by tests and the fixture builder)."""
    return {internal: a[public] for internal, public in _PUBLIC_FIELDS.items()}


class PublicAliasReader:
    """REFERENCE reader of the public contract (SPEC.md 8.12): how a consumer such as the PoE Checker is expected to
    turn an item's mod text into a stat id from `{game}/aliases/latest.json` ALONE -- the last rule of the chain; the
    earlier rules (exact table text, "(Local)", fixed numbers, plural, sign) are not in this file. Deliberately
    written against the readable public field names and independent of AliasIndex, so a change to the contract
    breaks a test instead of a consumer. Never raises on a malformed alias: it is skipped."""

    def __init__(self, doc: dict[str, Any]):
        self.deprecated = {str(n) for n in doc.get("deprecated_trade_ids", [])}
        self._implied: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._templates: list[tuple[re.Pattern, str, dict[str, Any]]] = []
        for a in doc.get("aliases", []):
            try:
                if a["implied_values"] is not None and not a["placeholder_stat_index"]:
                    for kind in a["kinds"]:
                        self._implied.setdefault((kind, a["text"]), []).append(a)
                else:
                    parts = _PLACE.split(a["text"])[::2]
                    rx = re.compile("".join([re.escape(parts[0])] + [f"({_NUM})" + re.escape(p) for p in parts[1:]]))
                    self._templates.append((rx, max(parts, key=len), a))
            except (KeyError, TypeError, IndexError, re.error):
                continue

    def candidates(self, kind: str, text: str) -> dict[str, tuple[list[float], bool]]:
        out: dict[str, tuple[list[float], bool]] = {}
        for a in self._implied.get((kind, text), ()):
            out.setdefault(str(a["trade_id"]), (list(a["implied_values"]), str(a["trade_id"]) in self.deprecated))
        for rx, literal, a in self._templates:
            if kind not in a["kinds"] or literal not in text:
                continue
            m = rx.fullmatch(text)
            if not m:
                continue
            displayed_by_stat = [0.0] * len(a["conditions"])
            for shown, stat in zip((float(g) for g in m.groups()), a["placeholder_stat_index"]):
                displayed_by_stat[stat] = shown
            ok = True
            for i, (lo, hi) in enumerate(a["conditions"]):
                internal = round(displayed_by_stat[i] * a["divisor"][i] * (-1 if a["negate"][i] else 1), 6)
                if (lo is not None and internal < lo) or (hi is not None and internal > hi):
                    ok = False
                    break
            if ok:
                out.setdefault(str(a["trade_id"]), ([displayed_by_stat[i] * a["sign"][i] for i in range(len(a["conditions"]))],
                                                    str(a["trade_id"]) in self.deprecated))
        return out

    def resolve(self, kind: str, text: str) -> Optional[tuple[str, list[float]]]:
        """(stat_id, stored values) when the alias table alone proves it, else None. Prefers a current stat over a
        deprecated-only one; two different trade ids left = ambiguous = None (never guess)."""
        cands = self.candidates(kind, text)
        current = {n: v for n, v in cands.items() if not v[1]}
        pool = current or cands
        if len(pool) != 1:
            return None
        num, (values, _dep) = next(iter(pool.items()))
        return f"{kind}.stat_{num}", values


def valid_public(doc: Any) -> bool:
    """Shape check the VM copy job applies before publishing a staged public file."""
    return (isinstance(doc, dict) and doc.get("schema_version") == PUBLIC_SCHEMA_VERSION
            and isinstance(doc.get("aliases"), list) and isinstance(doc.get("stat_groups"), list)
            and isinstance(doc.get("stat_aliases"), dict) and isinstance(doc.get("generated_at"), str))


def check_not_shrunk(new: dict[str, Any], previous: Optional[dict[str, Any]], min_ratio: float = 0.9, min_aliases: int = 1) -> None:
    """Guard for the refresh job: raise (so nothing is published over a good file) when the new payload
    is empty, or has far fewer aliases / deprecated ids than the previously published one."""
    n_new = len(new.get("aliases", []))
    if n_new < min_aliases:
        raise ValueError(f"new payload has {n_new} aliases (< {min_aliases})")
    if previous:
        for key in ("aliases", "deprecated"):
            old, cur = len(previous.get(key, [])), len(new.get(key, []))
            if old and cur < old * min_ratio:
                raise ValueError(f"{key} shrank from {old} to {cur} (< {min_ratio:.0%} of the last good file)")


class _Alias:
    __slots__ = ("num", "kinds", "regex", "literal", "text", "stat_of", "negate", "div", "sign", "cond", "implied", "deprecated")


class AliasIndex:
    """Runtime matcher built from a published payload. Safe by construction: an unreadable payload
    produces an empty index, never an exception."""

    def __init__(self, payload: Optional[dict[str, Any]] = None):
        self.deprecated: frozenset[str] = frozenset()
        self._implied: dict[tuple[str, str], list[_Alias]] = {}
        self._regex: dict[str, list[_Alias]] = {k: [] for k in KINDS}
        self.size = 0
        if payload:
            self._build(payload)

    @classmethod
    def empty(cls) -> "AliasIndex":
        return cls(None)

    @staticmethod
    def valid(payload: Any) -> bool:
        return isinstance(payload, dict) and payload.get("schema_version") == SCHEMA_VERSION and isinstance(payload.get("aliases"), list)

    def _build(self, payload: dict[str, Any]) -> None:
        if not self.valid(payload):
            return
        self.deprecated = frozenset(str(n) for n in payload.get("deprecated", []))
        for raw in payload["aliases"]:
            try:
                a = _Alias()
                a.num, a.kinds, a.text = str(raw["n"]), tuple(raw["k"]), raw["x"]
                a.negate, a.div, a.sign = raw["ng"], raw["dv"], raw["sg"]
                a.cond, a.implied, a.deprecated = raw["c"], raw["iv"], bool(raw.get("dep"))
                a.stat_of = raw["p"]
                if a.implied is not None and not a.stat_of:
                    for k in a.kinds:
                        self._implied.setdefault((k, a.text), []).append(a)
                else:
                    parts = _PLACE.split(a.text)[::2]
                    a.regex = re.compile("".join([re.escape(parts[0])] + [f"({_NUM})" + re.escape(p) for p in parts[1:]]))
                    a.literal = max(parts, key=len)
                    for k in a.kinds:
                        self._regex[k].append(a)
                self.size += 1
            except (KeyError, TypeError, IndexError, re.error):
                continue

    def candidates(self, kind: str, text: str) -> dict[str, tuple[list[float], bool]]:
        """{numeric GGG stat id: (stored values, deprecated?)} for every alias that renders exactly `text`."""
        out: dict[str, tuple[list[float], bool]] = {}
        for a in self._implied.get((kind, text), ()):
            out.setdefault(a.num, (list(a.implied), a.deprecated))
        for a in self._regex.get(kind, ()):
            if a.literal not in text:
                continue
            m = a.regex.fullmatch(text)
            if not m:
                continue
            shown = [float(g) for g in m.groups()]
            n_stats = len(a.cond)
            displayed = [0.0] * n_stats
            for g, stat in zip(shown, a.stat_of):
                displayed[stat] = g
            ok = True
            for i in range(n_stats):
                internal = round(displayed[i] * a.div[i] * (-1 if a.negate[i] else 1), 6)
                lo, hi = a.cond[i]
                if (lo is not None and internal < lo) or (hi is not None and internal > hi):
                    ok = False
                    break
            if ok:
                out.setdefault(a.num, ([displayed[i] * a.sign[i] for i in range(n_stats)], a.deprecated))
        return out
