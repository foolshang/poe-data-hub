"""Extract an insurance copy (`*.sqlite3.gz`) into a NEW file and give it the `snapshot_id` indexes the archive export
needs, so it can be used as `--extra-source` (which opens it `mode=ro`).

    python -m tools.prepare_archive_copy <copy.sqlite3.gz> <out.sqlite3>

The `.gz` is only read. The output must not exist (nothing is ever overwritten); the indexes are created on that fresh
extraction, never on the compressed original and never on the live database.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

INDEXES = (
    ("idx_skill_stat_counts_snapshot", "skill_stat_counts"),
    ("idx_skill_passive_counts_snapshot", "skill_passive_counts"),
    ("idx_skill_populations_snapshot", "skill_populations"),
)


def prepare(gz_path: Path, out_path: Path) -> None:
    gz_path, out_path = Path(gz_path), Path(out_path)
    if out_path.exists():
        raise SystemExit(f"refusing to overwrite {out_path}")
    with gzip.open(gz_path, "rb") as fin, open(out_path, "xb") as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    conn = sqlite3.connect(out_path)
    try:
        for name, table in INDEXES:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}(snapshot_id)")
        conn.commit()
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if check != "ok":
        raise SystemExit(f"integrity_check on {out_path}: {check}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"prepared {sys.argv[2]}")
