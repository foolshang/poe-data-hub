"""Measure where the memory of a skill publish goes, WITHOUT uploading anything and without writing the DB.

Mirrors main.publish_skill_meta step by step (same functions, same order) against a read-only SQLite handle and a
publisher that only serializes and discards. After each step it prints the process RSS, the high-water mark and
tracemalloc's current/peak, so "what held the memory" is a measurement, not a guess.

Run it under tools/run_capped.sh on the VM, never next to the live hub without a cap:
    sudo tools/run_capped.sh measure-pub 300 90 -- /opt/poe-data-hub/.venv/bin/python -m tools.measure_skill_publish poe2
"""
from __future__ import annotations

import gc
import json
import os
import sqlite3
import sys
import tracemalloc
from pathlib import Path

from src.processing.skill_publish import build_skill_groups, build_skills_index
from src.utils.config import load_config

DB_PATH = Path("data/poe_data_hub.sqlite3")


def rss_mb() -> tuple[float, float]:
    cur = hwm = 0.0
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            cur = int(line.split()[1]) / 1024
        elif line.startswith("VmHWM:"):
            hwm = int(line.split()[1]) / 1024
    return cur, hwm


def mark(label: str) -> None:
    cur, hwm = rss_mb()
    t_cur, t_peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (0, 0)
    print(f"{label:<46} rss={cur:6.1f}MB hwm={hwm:6.1f}MB | py-heap now={t_cur/1e6:6.1f}MB peak={t_peak/1e6:6.1f}MB", flush=True)


def main(game: str) -> None:
    cfg = load_config()
    min_usage = cfg.get("skill_meta", {}).get("publish", {}).get("min_usage_pct", 0.1)
    if os.environ.get("MSP_TRACEMALLOC"):          # off by default: it multiplies the memory of many small objects
        tracemalloc.start()
    mark("start")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA cache_size=-4000")
    run = conn.execute(
        "SELECT sr.id, sr.snapshot_id, sr.league, s.days_since_league_start FROM skill_runs sr "
        "JOIN snapshots s ON s.id = sr.snapshot_id "
        "WHERE sr.game = ? AND sr.status = 'finished' ORDER BY sr.id DESC LIMIT 1", (game,),
    ).fetchone()
    run_id, snap, league, days = run
    print(f"{game}: run {run_id} snapshot {snap}", flush=True)

    stat_rows = conn.execute(
        """SELECT skill_type, skill, level_bracket, slot, stat_id, mod_kind, count, population,
                  slot_holders, usage_pct, value_min, value_max FROM skill_stat_counts WHERE snapshot_id = ?""", (snap,)).fetchall()
    mark(f"fetchall stat rows ({len(stat_rows)} rows)")
    passive_rows = conn.execute(
        """SELECT skill_type, skill, level_bracket, keypassive, count, population, usage_pct
           FROM skill_passive_counts WHERE snapshot_id = ?""", (snap,)).fetchall()
    population_rows = conn.execute(
        """SELECT skill_type, skill, level_bracket, population, facet_count
           FROM skill_populations WHERE snapshot_id = ?""", (snap,)).fetchall()
    mark(f"fetchall passive ({len(passive_rows)}) + populations ({len(population_rows)})")
    kept = sum(1 for r in stat_rows if r[9] >= min_usage)
    print(f"   stat rows >= min_usage_pct {min_usage}: {kept} of {len(stat_rows)}", flush=True)

    groups = build_skill_groups(stat_rows, passive_rows, population_rows, min_usage)
    mark(f"build_skill_groups ({len(groups)} groups), rows still held")
    del stat_rows, passive_rows, population_rows
    gc.collect()
    mark("after del of the raw row lists + gc")

    index_data = build_skills_index(game, league, days, 0, groups)
    json.dumps(index_data, ensure_ascii=False, indent=2)
    mark("index serialized")

    biggest = 0
    for g in groups:
        payload = json.dumps(g.to_file(game, league, days), ensure_ascii=False, separators=(",", ":"))
        biggest = max(biggest, len(payload))
        del payload
    mark(f"all {len(groups)} skill files serialized one by one (largest {biggest/1e6:.2f}MB)")
    gc.collect()
    mark("end")


if __name__ == "__main__":
    main(sys.argv[1])
