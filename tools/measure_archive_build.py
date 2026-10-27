"""Measure the memory of one league-archive build WITHOUT writing the database.

    python -m tools.measure_archive_build new <db> <game> <league> <out.json>
    python -m tools.measure_archive_build old <db> <game> <league> <out.json>   # reference, loads the league in RAM

`new` is `archive_stream.build_league_archive_to_file` (read-only handle). `old` is the pre-rewrite
`archive_export.build_league_archive`, kept as the baseline of the comparison; it opens the database through
`LocalDb`, which runs the schema script, so point it at a scratch copy, never the production file.

Prints the process RSS before, after and at the high-water mark, plus tracemalloc's Python-heap peak, so
"how much did the build add" is a number: peak RSS minus the RSS right before the build started.
On the VM run it under tools/run_capped.sh (a hard cap), never next to the live hub without one.
"""
from __future__ import annotations

import json
import os
import sys
import time
import tracemalloc
from pathlib import Path


def rss_mb() -> tuple[float, float]:
    """(current, high-water) RSS in MB. /proc on Linux, psutil elsewhere."""
    proc = Path("/proc/self/status")
    if proc.exists():
        cur = hwm = 0.0
        for line in proc.read_text().splitlines():
            if line.startswith("VmRSS:"):
                cur = int(line.split()[1]) / 1024
            elif line.startswith("VmHWM:"):
                hwm = int(line.split()[1]) / 1024
        return cur, hwm
    import psutil
    info = psutil.Process().memory_info()
    return info.rss / 1048576, getattr(info, "peak_wset", info.rss) / 1048576


def main(argv: list[str]) -> int:
    if len(argv) != 6 or argv[1] not in ("new", "old"):
        print(__doc__)
        return 2
    mode, db_path, game, league, out = argv[1], Path(argv[2]), argv[3], argv[4], Path(argv[5])
    from src.processing.archive_export import build_league_archive
    from src.processing.archive_stream import build_league_archive_to_file
    from src.storage.local_db import LocalDb

    before, _ = rss_mb()
    print(f"before build                 rss={before:7.1f}MB", flush=True)
    trace = not os.environ.get("MEASURE_NO_TRACE")  # tracemalloc itself costs RSS; switch it off for the headline number
    if trace:
        tracemalloc.start()
    started = time.monotonic()
    if mode == "new":
        info = build_league_archive_to_file(db_path, game, league, out)
        print(f"leaves written               {info['leaves']}")
    else:
        db = LocalDb(db_path)
        data = build_league_archive(db, game, league)
        db.close()
        out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        del data
    seconds = time.monotonic() - started
    after, hwm = rss_mb()
    print(f"after build                  rss={after:7.1f}MB  high-water={hwm:7.1f}MB", flush=True)
    if trace:
        print(f"python-heap peak (tracemalloc) {tracemalloc.get_traced_memory()[1] / 1e6:7.1f}MB")
    print(f"ADDED over baseline          {max(hwm, after) - before:7.1f}MB   (high-water minus RSS before)")
    print(f"output {out.stat().st_size / 1e6:.1f}MB in {seconds:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
