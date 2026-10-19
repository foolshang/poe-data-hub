"""Build the alias-contract test cases (tests/fixtures/alias_contract/) from REAL data.

The cases are curated below (what each one demonstrates and whether it must map is a human decision); the
expected stat ids and values are read from the hub's own ModNormalizer run over the real GGG stats table plus the
real published aliases, then double-checked by an independent reader of the PUBLIC contract file
(`PublicAliasReader`) for the cases the alias table serves. The result is:

    tests/fixtures/alias_contract/cases.json         the cases (what the Checker's own tests should reproduce)
    tests/fixtures/alias_contract/subset_{game}.json the GGG table entries and public aliases those cases depend on,
                                                      so the CI test runs offline and deterministically
    tests/fixtures/alias_contract/CASES.md           the cases as a table (for humans / the Checker's authors)
    tests/fixtures/alias_contract/sample_poe1.json   ten real alias entries, one per kind of thing they can express

Usage (needs the real inputs, which live outside the repo):
    python -m tools.build_alias_contract_fixture --stats-poe1 P --stats-poe2 P --public-poe1 P --public-poe2 P
where the stats files are the GGG stats cache envelopes (`internal/stats_cache*.json`) and the public files are
`aliases_public_{game}.json` as staged by tools/refresh_rendering_aliases.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

from src.processing.normalizer import ModNormalizer
from src.processing.rendering_aliases import PublicAliasReader, from_public_alias
from tools.refresh_rendering_aliases import table_from_stats

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "alias_contract"

# (game, kind, text, category, why, stability)   stability: "by_design" | "may_change" (unmapped today, work pending)
CASES: list[tuple[str, str, str, str, str, str]] = [
    # ---- exact (the table's own wording): the baseline every consumer already has
    ("poe1", "explicit", "+88 to maximum Life", "exact", "the number is replaced by '#'; the table text is '+# to maximum Life'", ""),
    ("poe1", "explicit", "Historic", "exact", "no number at all: exact text match, no values", ""),
    ("poe2", "implicit", "20% of Flask Recovery applied Instantly", "exact", "PoE2 exact match", ""),
    # ---- (Local) suffix: the table says '(Local)', the item does not
    ("poe1", "explicit", "100% increased Energy Shield", "local_suffix", "table text is '#% increased Energy Shield (Local)'; the item never prints '(Local)'", ""),
    ("poe1", "explicit", "100% increased Armour and Evasion", "local_suffix", "same rule, different stat", ""),
    ("poe1", "explicit", "333% increased Armour and Energy Shield", "local_suffix", "same rule, three-word stat name", ""),
    ("poe1", "explicit", "+45 to maximum Energy Shield", "local_ambiguous_stays_global", "the table has BOTH '+# to maximum Energy Shield' (global) and '... (Local)': the item text cannot say which, so the exact (global) match is kept, never the Local twin", ""),
    # ---- fixed numbers baked into the table text: different numbers are different stats
    ("poe1", "implicit", "Has 1 Socket", "fixed_number", "'Has 1 Socket' is its own table stat; the 1 is not a roll", ""),
    ("poe1", "explicit", "Has 2 Sockets", "fixed_number_alias", "not in the table at all; RePoE proves it is the same stat as 'Has 1 Socket' with the value 2", ""),
    ("poe1", "explicit", "Has 3 Sockets", "fixed_number_alias", "same stat, value 3", ""),
    ("poe1", "implicit", "3% increased Damage per 100 Strength", "fixed_number", "'per 100 Strength' and 'per 15 Strength' are DIFFERENT stats; only the first number is the roll", ""),
    ("poe1", "explicit", "2% increased Damage per 15 Strength", "fixed_number", "the other stat of the pair above", ""),
    ("poe1", "explicit", "Attacks have 150% Arcane Might", "fixed_number", "'Attacks have 100% Arcane Might' is a different stat: collapsing numbers to '#' would merge them", ""),
    ("poe1", "explicit", "Adds 1 to 80 Chaos Damage to Attacks per 80 Strength", "fixed_number", "two rolls (1, 80) and one fixed number (80)", ""),
    # ---- singular / plural
    ("poe1", "explicit", "Gain 3 Charges when you are Hit by an Enemy", "plural", "table lists the singular 'Gain # Charge ...'", ""),
    ("poe1", "explicit", "Has 1 Abyssal Socket", "plural", "table lists the plural 'Has # Abyssal Sockets'", ""),
    ("poe1", "explicit", "Gain Adrenaline for 3 seconds on Kill", "plural", "'second' in the table, 'seconds' on the item", ""),
    ("poe1", "implicit", "-2 Prefix Modifiers allowed", "plural_negative", "plural AND a negative roll of '+# Prefix Modifier allowed'", ""),
    ("poe1", "explicit", "Bow Attacks fire an additional Arrow", "plural_alias", "the 'an' wording carries no number: RePoE's condition [1..1] gives the value 1", ""),
    ("poe1", "explicit", "You can apply an additional Curse", "plural_alias", "same shape", ""),
    ("poe1", "explicit", "Skills fire 2 additional Projectiles during Effect", "plural_alias", "table lists 'Skills fire an additional Projectile during Effect' (value 1 baked in)", ""),
    ("poe2", "implicit", "+2 Prefix Modifiers allowed", "plural", "PoE2: table lists '# Prefix Modifier allowed'", ""),
    # ---- negate: increased <-> reduced, more <-> less (stored value is negative)
    ("poe1", "explicit", "29% reduced Duration", "negate", "the table only lists '#% increased Duration'; 'reduced' is the same stat with a negative value", ""),
    ("poe1", "implicit", "20% reduced Bleed Duration on you", "negate", "same rule on an implicit", ""),
    ("poe1", "explicit", "45% less Duration", "negate", "'less' mirrors 'more'", ""),
    ("poe1", "explicit", "-30% to Fire Resistance", "negative_of_plus", "table '+#% to Fire Resistance' with a negative roll", ""),
    ("poe1", "explicit", "30% reduced Mana Burn rate", "exact", "the table itself lists the 'reduced' wording (value 30, positive): this side of Mana Burn maps; the 'increased' side is the unmapped case below", ""),
    # ---- explicit plus on the item selects the template that spells the plus
    ("poe1", "implicit", "+25% Chance to Block Spell Damage", "explicit_plus", "RePoE: the '+#' stat prints a plus; the plain '#%' stat does not", ""),
    ("poe1", "explicit", "+6% Chance to Block Attack Damage", "explicit_plus", "plus on the item -> the '+#%' template", ""),
    ("poe1", "implicit", "6% Chance to Block Attack Damage", "explicit_plus", "no plus on the item -> the plain '#%' template (a different stat id)", ""),
    # ---- text without a number
    ("poe1", "explicit", "Chance to Block is Lucky", "no_number", "table lists 'Chance to Block is Unlucky'; Lucky is the mirrored sign, nothing is printed, so no value", ""),
    ("poe1", "explicit", "Ignore Stuns while using Socketed Attack Skills", "no_number", "the 100% case of '#% chance to Ignore Stuns ...': value 100 is implied by RePoE's [100..] condition", ""),
    # ---- units
    ("poe1", "explicit", "Gain a Void Charge every second", "units", "table 'every # seconds'; RePoE converts milliseconds: the value is 1 second, NOT 1000", ""),
    ("poe1", "explicit", "Effects of Consecrated Ground you create Linger for 4 seconds", "units", "item shows seconds, the table text bakes in '1 second': value 4", ""),
    ("poe1", "explicit", "Eat a Soul when you Hit a Rare or Unique Enemy, no more than once every 0.5 seconds", "units", "a decimal number of seconds", ""),
    ("poe1", "explicit", "Trigger a Socketed Bow Skill when you Attack with a Bow, with a 1 second Cooldown", "units", "the '100% chance to ...' wording without its percentage: value 100", ""),
    # ---- cluster jewel notables / passive text
    ("poe1", "explicit", "1 Added Passive Skill is Martial Prowess", "cluster_notable", "the notable's name is fixed text: exact match, no values", ""),
    ("poe1", "explicit", "Added Small Passive Skills also grant: +5% to Cold Resistance", "cluster_small", "cluster small-passive line", ""),
    # ---- stat ids the GGG table cannot tell apart (see stat_groups in the contract)
    ("poe1", "explicit", "Socketed Gems are supported by Level 20 Multistrike", "stat_group_case_only", "two table stats differ only in the case of 'supported': the hub emits the one whose casing matches the item; the Checker must look in stat_groups", ""),
    # ---- must NOT map: the expected result is 'unmapped', not an omission
    ("poe1", "explicit", "66% increased Mana Burn rate", "must_not_map", "only the 'reduced' wording is in the data: the 'increased' side is not provable (allowlisted gap)", "by_design"),
    ("poe1", "explicit", "Trigger Level 1 Create Lesser Shrine when you Kill an Enemy", "must_not_map", "RePoE condition is [None..None] (default branch, value unknown): never mapped with an invented value", "by_design"),
    ("poe1", "explicit", "Impale Enemies on Hit with Attacks", "must_not_map", "two GGG trade ids (725880290, 3739863694) carry this wording identically: ambiguous, so neither", "by_design"),
    ("poe1", "explicit", "Projectiles Pierce an additional Target", "must_not_map", "one trade id shared by four internal stats whose stored values differ: the value is not provable", "by_design"),
    ("poe1", "explicit", "50% of Chaos Damage taken does not bypass Energy Shield", "must_not_map", "two GGG trade ids render this identically: ambiguous", "by_design"),
    ("poe1", "explicit", "Projectiles Return to you", "must_not_map", "ambiguous between several stats in the data", "by_design"),
    ("poe1", "explicit", "+5% Chance to Block", "must_not_map", "shield-local block chance: needs slot context (local vs global); planned, so this may map later", "may_change"),
    ("poe2", "explicit", "Cannot Leech Mana", "must_not_map", "PoE2 wording absent from the table and not provable from RePoE", "may_change"),
    # ---- PoE2
    ("poe2", "explicit", "Lose 2% of maximum Life on Kill", "negate_alias", "table lists 'Recover #% of maximum Life on Kill': the 'Lose' wording is its negation", ""),
    ("poe2", "implicit", "Grants 1 additional Skill Slot", "fixed_number", "PoE2 fixed number baked into the table text", ""),
]


def _skel(text: str) -> str:
    t = text.casefold().replace("(local)", "")
    t = re.sub(r"\{\d*\}|[+-]?\d+(?:\.\d+)?|#", "#", t)
    t = re.sub(r"[+-]#", "#", t)
    return " ".join(w[:-1] if w.endswith("s") and len(w) > 3 else w for w in t.split())


class _NoHttp:
    def get_json(self, url):
        raise RuntimeError("offline")


def _normalizer(game: str, stats_envelope: dict, internal_aliases: dict) -> ModNormalizer:
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "stats.json"
    cache.write_text(json.dumps({"fetched_at": 4102444800, "data": stats_envelope["data"] if "data" in stats_envelope else stats_envelope}), encoding="utf-8")
    (tmp / f"rendering_aliases_{game}.json").write_text(json.dumps(internal_aliases), encoding="utf-8")
    n = ModNormalizer(http_client=_NoHttp(), game=game, cache_path=cache, unmapped_log_path=tmp / "u.log")
    n.load()
    return n


def _internal_doc(game: str, public: dict) -> dict:
    return {"schema_version": 1, "game": game, "generated_at": public["generated_at"], "stats": {},
            "deprecated": public["deprecated_trade_ids"], "aliases": [from_public_alias(a) for a in public["aliases"]]}


# (label, what it demonstrates, selector over a real public alias)
_SAMPLE_PICKS = [
    ("sign-only wording", "no placeholder, nothing printed on the item: implied_values is [] (no value to store)", lambda a: a["text"] == "Chance to Block is Lucky"),
    ("implied value", "no placeholder, but the value is proven by RePoE (here 1 second): implied_values [1.0], not 1000", lambda a: a["text"] == "Gain a Void Charge every second"),
    ("singular 'Socket' family", "a placeholder wording that exists only for some values (conditions [2, null] = 2 or more)", lambda a: a["text"] == "Has {0} Sockets"),
    ("negated placeholder", "the item prints 'reduced'; the stored value is negative: negate [true], sign [-1]", lambda a: a["text"].startswith("{0}% reduced Duration of Poisons")),
    ("millisecond unit", "divisor 1000: the item shows seconds, RePoE's internal value is milliseconds (conditions are on the INTERNAL value)", lambda a: a["divisor"] == [1000] and a["placeholder_stat_index"] == [0]),
    ("percent unit", "divisor 100: displayed value x 100 = internal value", lambda a: a["divisor"] == [100] and a["placeholder_stat_index"] == [0] and len(a["kinds"]) > 1),
    ("several kinds", "the same stat exists as explicit and implicit in the GGG table", lambda a: len(a["kinds"]) == 2 and a["placeholder_stat_index"]),
    ("two values", "two placeholders: placeholder_stat_index says which stat's value each one shows", lambda a: len(a["conditions"]) == 2),
    ("plural placeholder", "'{0} additional Curses ...' only for 2 or more; the singular wording is another entry or the table text", lambda a: a["text"].startswith("{0} additional Curses")),
    ("deprecated-only trade id", "deprecated true: a consumer prefers a current stat over this one", lambda a: a["deprecated"] is True),
]


def _write_sample(public_path: Path) -> None:
    doc = json.loads(public_path.read_text(encoding="utf-8"))
    picked = []
    for label, why, pred in _SAMPLE_PICKS:
        a = next((x for x in doc["aliases"] if pred(x)), None)
        if a is None:
            raise SystemExit(f"sample selector found nothing for {label!r}")
        picked.append({"demonstrates": label, "explanation": why, "alias": a})
    group_ex = [next(g for g in doc["stat_groups"] if g["reason"] == r) for r in ("identical_text", "case_only")]
    (OUT / "sample_poe1.json").write_text(json.dumps({
        "about": "Ten REAL entries of poe1/aliases/latest.json (generated_at %s), one per kind of thing an alias can express, plus two real stat_groups entries." % doc["generated_at"],
        "entries": picked, "stat_groups_examples": group_ex}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _write_cases_markdown(cases: list[dict]) -> None:
    lines = ["# Alias contract test cases", "",
             "Generated by `tools/build_alias_contract_fixture.py` from `cases.json`; the JSON is the source of truth. "
             "`unmapped` is an EXPECTED result. `may_change` = unmapped today, the hub plans to map it later.", "",
             "| id | kind | mod text on the item | expected stat_id | values | category | why |", "|---|---|---|---|---|---|---|"]
    for c in cases:
        e = c["expect"]
        text = c["text"].replace("\n", " / ").replace("|", "\\|")
        if e["status"] == "mapped":
            lines.append(f"| {c['id']} | {c['kind']} | `{text}` | `{e['stat_id']}` | {e['values']} | {c['category']} | {c['why']} |")
        else:
            lines.append(f"| {c['id']} | {c['kind']} | `{text}` | **UNMAPPED** ({e['stability']}) | - | {c['category']} | {c['why']} |")
    (OUT / "CASES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(args: argparse.Namespace) -> int:
    logging.disable(logging.CRITICAL)
    OUT.mkdir(parents=True, exist_ok=True)
    cases_out: list[dict] = []
    subsets: dict[str, dict] = {}
    for game, stats_path, public_path in (("poe1", args.stats_poe1, args.public_poe1), ("poe2", args.stats_poe2, args.public_poe2)):
        envelope = json.loads(Path(stats_path).read_text(encoding="utf-8"))
        data = envelope["data"] if "data" in envelope else envelope
        table = table_from_stats({"data": data})
        public = json.loads(Path(public_path).read_text(encoding="utf-8"))
        full = _normalizer(game, {"data": data}, _internal_doc(game, public))
        reader = PublicAliasReader(public)
        text_of = {i: t for kind in table.values() for i, t in kind.items()}

        needed_ids: set[str] = set()
        needed_nums: set[str] = set()
        game_cases = [c for c in CASES if c[0] == game]
        results = []
        for _g, kind, text, category, why, stability in game_cases:
            hits_before = full.alias_hits
            got = full.normalize(text, kind)
            alias_rule_fired = full.alias_hits > hits_before          # the alias table is only the LAST rule of the chain
            mapped = [(m.stat_id, m.values) for m in got]
            if len(mapped) > 1:
                print(f"ERROR {game} multi-stat result for {text!r}", file=sys.stderr)
                return 1
            results.append((kind, text, category, why, stability, mapped[0] if mapped else None, alias_rule_fired))
            sk = _skel(text)
            for k, entries in table.items():
                for sid, t in entries.items():
                    if _skel(t) == sk:
                        needed_ids.add(sid)
            if mapped:
                needed_ids.add(mapped[0][0])
            for num in full._aliases.candidates(kind, text):
                needed_nums.add(num)
        for sid in list(needed_ids):
            needed_nums.add(sid.split("_")[-1])
        # every table entry that shares a numeric trade id with a needed one (other kinds / duplicates)
        for k, entries in table.items():
            for sid in entries:
                if sid.split("_")[-1] in needed_nums:
                    needed_ids.add(sid)
        sub_table: dict[str, dict[str, str]] = {}
        for k, entries in table.items():
            for sid, t in entries.items():
                if sid in needed_ids:
                    sub_table.setdefault(k, {})[sid] = t
        sub_aliases = [a for a in public["aliases"] if a["trade_id"] in needed_nums]
        sub_public = {**public, "aliases": sub_aliases,
                      "stat_groups": [g for g in public["stat_groups"] if set(g["members"]) & needed_ids],
                      "stat_aliases": {m: c for m, c in public["stat_aliases"].items() if m in needed_ids or c in needed_ids}}
        subsets[game] = {"table": sub_table, "public": sub_public}

        for kind, text, category, why, stability, mapped, alias_rule_fired in results:
            case = {"id": f"{game}-{len(cases_out) + 1:02d}", "game": game, "kind": kind, "text": text, "category": category, "why": why}
            if mapped:
                sid, values = mapped
                case["expect"] = {"status": "mapped", "stat_id": sid, "values": values, "table_text": text_of.get(sid)}
                group = next((g for g in public["stat_groups"] if sid in g["members"]), None)
                if group:
                    case["stat_group"] = {"reason": group["reason"], "canonical": group["canonical"], "members": group["members"]}
                if alias_rule_fired:      # the alias table is what produced it: the independent reader must agree exactly
                    rid, rvalues = reader.resolve(kind, text) or (None, [])
                    if rid != sid or [round(v, 6) for v in rvalues] != [round(v, 6) for v in values]:
                        print(f"ERROR {game} public reader disagrees on {text!r}: {rid} {rvalues} vs {sid} {values}", file=sys.stderr)
                        return 1
                    case["served_by_alias_table"] = True
            else:
                case["expect"] = {"status": "unmapped", "stability": stability or "by_design"}
                # NOT checked against the reader: an earlier rule may legitimately refuse where the alias table alone
                # would answer (the chain order is part of the contract)
            cases_out.append(case)

        # the reduced subset must reproduce every full-data result, or the fixture is incomplete
        mini_envelope = {"data": {"result": [{"id": k, "entries": [{"id": i, "text": t} for i, t in e.items()]} for k, e in sub_table.items()]}}
        mini = _normalizer(game, mini_envelope, _internal_doc(game, sub_public))
        for case in (c for c in cases_out if c["game"] == game):
            got = mini.normalize(case["text"], case["kind"])
            exp = case["expect"]
            ok = (not got) if exp["status"] == "unmapped" else (len(got) == 1 and got[0].stat_id == exp["stat_id"] and got[0].values == exp["values"])
            if not ok:
                print(f"ERROR {game} subset does not reproduce {case['id']} {case['text']!r}: {[(m.stat_id, m.values) for m in got]}", file=sys.stderr)
                return 1

    _write_sample(Path(args.public_poe1))
    _write_cases_markdown(cases_out)
    (OUT / "cases.json").write_text(json.dumps({
        "format": "alias-contract-cases/1",
        "about": "Mod text -> expected result of the hub's ModNormalizer (SPEC.md 8.12). 'unmapped' is an EXPECTED result, not "
                 "an omission; stability 'may_change' means the hub plans to map it later.",
        "cases": cases_out}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for game, sub in subsets.items():
        (OUT / f"subset_{game}.json").write_text(json.dumps(sub, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(cases_out)} cases; subsets: " + ", ".join(f"{g} {sum(len(v) for v in s['table'].values())} table entries / {len(s['public']['aliases'])} aliases" for g, s in subsets.items()))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    for name in ("stats-poe1", "stats-poe2", "public-poe1", "public-poe2"):
        ap.add_argument(f"--{name}", required=True)
    sys.exit(build(ap.parse_args()))


if __name__ == "__main__":
    main()
