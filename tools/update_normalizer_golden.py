"""Golden file of ModNormalizer results (drift guard, layer 1) -- create / update / check.

WHY. The PoE Checker mirrors normalizer rules 1-5 in `stat_match.ts`. A change to those rules in `normalizer.py` must
reach the Checker, and must never happen by accident. `tests/golden/{game}/` freezes (1) the GGG table entries,
(2) the rendering aliases (the PUBLIC contract file, SPEC.md 8.12) and (3) ~35,000 real item mod texts together with the result the normalizer gave for each.
`tests/test_normalizer_golden.py` replays them and FAILS when any result changes, so a rule change always shows up as a
diff of `expected.json.gz` in the same commit; nobody has to remember anything. The Checker's scheduled CI replays the
same files through `stat_match.ts` (tests/golden/README.md).

The inputs are FROZEN: updating the expected results never reads the network or the live data, so the file changes only
when the code does. Refreshing the inputs (newer table / aliases / corpus) is a separate, explicit command.

Usage:
    python -m tools.update_normalizer_golden                       # recompute expected results from the frozen inputs
    python -m tools.update_normalizer_golden --check               # exit 1 if they differ (what the test does)
    python -m tools.update_normalizer_golden --create GAME --stats STATS_ENVELOPE.json --aliases ALIASES_PUBLIC.json \
                                              --corpus mod_sample.json    # freeze NEW inputs (then review the diff)
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.processing.normalizer import ModNormalizer
from src.processing.rendering_aliases import from_public_alias

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden"
KINDS = ("explicit", "implicit", "fractured")


class _NoHttp:
    def get_json(self, url):
        raise RuntimeError("offline")


def _read_gz(path: Path) -> Any:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def _write_gz(path: Path, obj: Any) -> None:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))        # mtime=0: byte-identical output for identical input


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _internal_aliases(game: str, public: dict[str, Any]) -> dict[str, Any]:
    """The hub reads its private compact alias format; the golden stores the PUBLIC contract file (SPEC.md 8.12) so a
    consumer's test uses exactly what it reads in production."""
    return {"schema_version": 1, "game": game, "generated_at": public["generated_at"], "stats": {},
            "deprecated": public["deprecated_trade_ids"], "aliases": [from_public_alias(a) for a in public["aliases"]]}


def make_normalizer(game: str, table: dict[str, dict[str, str]], aliases: dict[str, Any]) -> ModNormalizer:
    """A ModNormalizer over the frozen table + (public-format) aliases, fully offline."""
    logging.disable(logging.CRITICAL)
    tmp = Path(tempfile.mkdtemp())
    envelope = {"fetched_at": 4102444800, "data": {"result": [
        {"id": kind, "entries": [{"id": i, "text": t} for i, t in entries.items()]} for kind, entries in table.items()]}}
    (tmp / "stats.json").write_text(json.dumps(envelope), encoding="utf-8")
    (tmp / f"rendering_aliases_{game}.json").write_text(json.dumps(_internal_aliases(game, aliases)), encoding="utf-8")
    n = ModNormalizer(http_client=_NoHttp(), game=game, cache_path=tmp / "stats.json", unmapped_log_path=tmp / "u.log")
    n.load()
    return n


def replay(game: str, directory: Path, cases: list[list[Any]] | None = None) -> tuple[list[list[Any]], list[list[Any]]]:
    """(cases, results) where results[i] = [[stat_id, values], ...] for cases[i] = [kind, text]."""
    table = _read_gz(directory / "table.json.gz")
    aliases = _read_gz(directory / "aliases.json.gz")
    n = make_normalizer(game, table, aliases)
    if cases is None:
        cases = [[k, t] for k, t, _r in _read_gz(directory / "expected.json.gz")["cases"]]
    return cases, [[[m.stat_id, m.values] for m in n.normalize(text, kind)] for kind, text in cases]


def write_expected(game: str, directory: Path, cases: list[list[Any]], results: list[list[Any]]) -> None:
    _write_gz(directory / "expected.json.gz", {
        "format": "normalizer-golden/1", "game": game,
        "inputs_sha256": {"table.json.gz": _sha(directory / "table.json.gz"), "aliases.json.gz": _sha(directory / "aliases.json.gz")},
        "mapped": sum(1 for r in results if r), "unmapped": sum(1 for r in results if not r),
        "cases": [[k, t, r] for (k, t), r in zip(cases, results)],
    })


def create(game: str, stats_path: Path, aliases_path: Path, corpus_path: Path) -> None:
    from tools.refresh_rendering_aliases import table_from_stats

    directory = GOLDEN / game
    directory.mkdir(parents=True, exist_ok=True)
    envelope = json.loads(stats_path.read_text(encoding="utf-8"))
    table = {k: v for k, v in table_from_stats(envelope).items() if k in KINDS}
    aliases = json.loads(aliases_path.read_text(encoding="utf-8"))
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))[f"{game}_legacy"]
    distinct = sorted({(kind, text) for by_slot in corpus for rows in by_slot.values() for kind, text in rows if kind in KINDS})
    _write_gz(directory / "table.json.gz", table)
    _write_gz(directory / "aliases.json.gz", aliases)
    cases, results = replay(game, directory, [list(c) for c in distinct])
    write_expected(game, directory, cases, results)
    print(f"{game}: {len(cases)} texts frozen ({sum(1 for r in results if r)} mapped, {sum(1 for r in results if not r)} unmapped)")


def diff(game: str, directory: Path) -> list[str]:
    """Human-readable differences between the stored expected results and a fresh replay ([] = identical)."""
    stored = _read_gz(directory / "expected.json.gz")
    problems: list[str] = []
    for name, sha in stored["inputs_sha256"].items():
        if _sha(directory / name) != sha:
            problems.append(f"{name} was changed without regenerating expected.json.gz")
    cases = [[k, t] for k, t, _r in stored["cases"]]
    _cases, fresh = replay(game, directory, cases)
    buckets: dict[str, list[str]] = {"mapped -> UNMAPPED": [], "unmapped -> MAPPED": [], "different stat_id": [], "different values": []}
    for (kind, text, old), new in zip(stored["cases"], fresh):
        if old == new:
            continue
        if old and not new:
            key = "mapped -> UNMAPPED"
        elif new and not old:
            key = "unmapped -> MAPPED"
        elif [r[0] for r in old] != [r[0] for r in new]:
            key = "different stat_id"
        else:
            key = "different values"
        buckets[key].append(f"{kind}|{text[:90]!r}: {old} -> {new}")
    for key, items in buckets.items():
        if items:
            problems.append(f"{len(items)} x {key}; first: " + "; ".join(items[:5]))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--create", choices=("poe1", "poe2"))
    ap.add_argument("--stats")
    ap.add_argument("--aliases")
    ap.add_argument("--corpus")
    a = ap.parse_args()
    if a.create:
        if not (a.stats and a.aliases and a.corpus):
            ap.error("--create needs --stats, --aliases and --corpus")
        create(a.create, Path(a.stats), Path(a.aliases), Path(a.corpus))
        return 0
    bad = 0
    for game in ("poe1", "poe2"):
        directory = GOLDEN / game
        problems = diff(game, directory)
        if a.check:
            for p in problems:
                print(f"{game}: {p}")
            bad += bool(problems)
        else:
            cases, results = replay(game, directory)
            write_expected(game, directory, cases, results)
            print(f"{game}: expected results rewritten ({len(problems)} kind(s) of difference before the rewrite)")
            for p in problems:
                print("   ", p)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
