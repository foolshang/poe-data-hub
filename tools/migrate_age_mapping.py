"""Re-sort an archive state accumulated under the old age-bucket rule (see `src/processing/archive_migrate.py`).

    python -m tools.migrate_age_mapping --state <state.sqlite3> --game poe1 --league Allflame \
        --source copy1=<prepared copy.sqlite3> [--source live=<db>] [--apply]

Without `--apply` it only prints the plan (which snapshots move, which buckets are recomputed, which sources it would read) and
changes nothing. With `--apply` it first keeps `<state>.pre-age-mapping-1` beside the state, then does the whole change in one
transaction. Sources are opened read-only; they must carry `snapshot_id` indexes (`tools/prepare_archive_copy.py`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from src.processing.archive_migrate import migrate_state
from src.processing.archive_skills import ArchiveStateError, Source


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--game", required=True)
    ap.add_argument("--league", required=True)
    ap.add_argument("--source", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    sources = []
    for item in args.source:
        name, _, path = item.partition("=")
        if not name or not path or not Path(path).exists():
            print(f"bad --source {item!r} (NAME=PATH, the file must exist)", file=sys.stderr)
            return 2
        sources.append(Source(name, Path(path)))
    sha_before = _sha(args.state) if args.state.exists() else None
    try:
        report = migrate_state(args.state, sources, args.game, args.league, apply=args.apply)
    except ArchiveStateError as e:
        print(f"REFUSED, nothing changed: {e}", file=sys.stderr)
        return 1
    out = {
        "mode": "applied" if report.applied else "dry-run (nothing changed)",
        "game": report.game, "league": report.league,
        "moved_snapshots": {str(k): list(v) for k, v in sorted(report.moved.items())},
        "recomputed_buckets": report.recomputed_buckets, "added_only_buckets": report.added_only_buckets,
        "snapshots_read": {str(k): v for k, v in sorted(report.snapshots_read.items())},
        "totals_before": report.totals_before, "totals_after": report.totals_after,
        "state_sha256_before": sha_before, "state_sha256_after": _sha(args.state),
        "backup": str(report.backup) if report.backup else None,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
