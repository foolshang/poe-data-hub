"""Compare two published `{game}/meta/latest.json` files (before / after a normalizer change).

Per slot (archetype "all"): how the top-10 stats changed, how many stats exist, and how the badge-relevant sets
changed (what the PoE Checker colours red / gold is a function of usage_pct and rank_in_slot, so membership of the
top ranks is what matters). Read-only; takes two local files.

    python -m tools.compare_meta_rank BEFORE.json AFTER.json [--slot body] [--top 10]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: str) -> tuple[dict, dict[str, str]]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {e["stat_id"]: e["template"] for e in d.get("stat_dictionary", [])}
    return d, names


def by_slot(d: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in d["mods"]:
        if m.get("archetype") == "all":
            out.setdefault(m["slot"], []).append(m)
    for rows in out.values():
        rows.sort(key=lambda r: r["rank_in_slot"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--slot", default="body")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    (bd, bn), (ad, an) = load(a.before), load(a.after)
    names = {**bn, **an}
    b, n = by_slot(bd), by_slot(ad)

    print(f"{'slot':<10}{'stats before':>13}{'after':>8}{'top10 same set':>16}{'top10 same order':>18}{'top25 members changed':>23}")
    for slot in sorted(set(b) | set(n)):
        rb, ra = b.get(slot, []), n.get(slot, [])
        t10b, t10a = [r["stat_id"] for r in rb[:10]], [r["stat_id"] for r in ra[:10]]
        t25b, t25a = {r["stat_id"] for r in rb[:25]}, {r["stat_id"] for r in ra[:25]}
        print(f"{slot:<10}{len(rb):>13}{len(ra):>8}{str(set(t10b) == set(t10a)):>16}{str(t10b == t10a):>18}{len(t25b ^ t25a) // 2 if t25b ^ t25a else 0:>23}")

    for label, rows in (("BEFORE", b.get(a.slot, [])), ("AFTER", n.get(a.slot, []))):
        print(f"\n{a.slot} top {a.top} {label}")
        for r in rows[: a.top]:
            print(f"  {r['rank_in_slot']:>3} {r['usage_pct']:>6.1f}%  {names.get(r['stat_id'], r['stat_id'])[:70]}")


if __name__ == "__main__":
    main()
