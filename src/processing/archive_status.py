"""What the league archive protects, for `status.json` (and from there the heartbeat).

The three skill tables are deleted by retention 45 days after a snapshot's `created_at` (`LocalDb.cleanup_old_skill_stat_counts`,
daily cleanup job). A snapshot's skill-centric rows therefore survive only if they are in the archive's accumulation
(`merged_snapshots` of the skills state file) or in an insurance copy (`archive.extra_sources`) when that cleanup runs.
`compute_archive_coverage` answers, per league, which finished snapshots with rows still in the live database are in
NEITHER, and when the first of them will be deleted. Everything is opened `mode=ro`; probes use the `snapshot_id` indexes
(`EXISTS ... LIMIT 1`), never a count or a scan.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from src.processing.archive_runner import archive_paths
from src.processing.archive_skills import SourceNotIndexedError, Source, _has_rows, _require_snapshot_indexes, _scan_source
from src.processing.archive_stream import open_readonly
from src.storage.local_db import COLLECTION_METHOD_SKILL

log = logging.getLogger(__name__)

_FMT = "%Y-%m-%dT%H:%M:%SZ"
MAX_LISTED = 20


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_FMT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _FMT).replace(tzinfo=timezone.utc)


def retention_delete_at(created_at: str, retention_days: int = 45, cleanup_hour_utc: int = 21) -> datetime:
    """When the daily cleanup first deletes the rows of a snapshot taken at `created_at`: the first cleanup run after
    `created_at + retention_days` (the cleanup deletes rows older than `now - retention_days`)."""
    due = _parse(created_at) + timedelta(days=retention_days)
    run = due.replace(hour=cleanup_hour_utc, minute=0, second=0, microsecond=0)
    return run if run > due else run + timedelta(days=1)


def _leagues(conn) -> list[tuple[str, str]]:
    return [(g, l) for g, l in conn.execute(
        "SELECT DISTINCT game, league FROM snapshots WHERE collection_method = ? ORDER BY game, league",
        (COLLECTION_METHOD_SKILL,))]


def _merged_ids(state_path: Path) -> tuple[Optional[set[int]], Optional[str]]:
    """(ids, error). A state file that is absent is `(set(), None)`; one that cannot be read is `(None, message)`."""
    if not state_path.exists():
        return set(), None
    try:
        conn = open_readonly(state_path)
        try:
            return {r[0] for r in conn.execute("SELECT snapshot_id FROM merged_snapshots")}, None
        finally:
            conn.close()
    except Exception as e:  # sqlite3.Error, OSError
        return None, f"{type(e).__name__}: {e}"


def _servable(conn, game: str, league: str, cutoff: str) -> dict[int, str]:
    """snapshot id -> created_at of the finished snapshots of the league that still have rows in `conn`."""
    return {sid: st.created_at for sid, st in _scan_source(conn, game, league, cutoff).items()
            if st.status == "finished" and _has_rows(conn, sid)}


def compute_archive_coverage(
    db_path: Path, archive_dir: Path, copies: Sequence[Path] = (), now: Optional[datetime] = None,
    retention_days: int = 45, cleanup_hour_utc: int = 21, resume_max_age_hours: float = 48.0,
) -> dict[str, Any]:
    """See the module docstring. Shape:

    {"computed_at", "earliest_loss_at" (or None), "copies": [{"name", "status": "ok"|"missing"|"not_indexed"|"error"}],
     "leagues": [{"game", "league", "live_snapshots_with_rows", "archive_merged" (None = state unreadable),
                  "archive_state_error" (only if unreadable), "backup_covers_through": {"snapshot_id", "created_at", "copy"} | None,
                  "at_risk_count", "at_risk": [{"snapshot_id", "created_at", "retention_delete_at"}] (first MAX_LISTED),
                  "earliest_loss_at", "lost_count", "lost_snapshot_ids" (first MAX_LISTED)  # finished, in no source, no rows left }]}
    """
    now = now or datetime.now(timezone.utc)
    cutoff = _iso(now - timedelta(hours=resume_max_age_hours))
    copy_conns: list[tuple[str, Any]] = []
    copy_report: list[dict[str, str]] = []
    live = open_readonly(Path(db_path))
    try:
        for i, p in enumerate(copies):
            p = Path(p)
            entry = {"name": p.name, "status": "ok"}
            copy_report.append(entry)
            if not p.exists():
                entry["status"] = "missing"
                continue
            try:
                conn = open_readonly(p)
            except Exception as e:
                entry.update(status="error", error=f"{type(e).__name__}: {e}")
                continue
            try:
                _require_snapshot_indexes(conn, Source(f"copy{i + 1}", p))
            except SourceNotIndexedError:
                entry["status"] = "not_indexed"
                conn.close()
                continue
            except Exception as e:
                entry.update(status="error", error=f"{type(e).__name__}: {e}")
                conn.close()
                continue
            copy_conns.append((p.name, conn))

        leagues_out: list[dict[str, Any]] = []
        overall: Optional[datetime] = None
        for game, league in _leagues(live):
            # every finished snapshot of the league, including those whose rows are already gone from the live database
            all_finished = {sid: st.created_at for sid, st in _scan_source(live, game, league, cutoff).items()
                            if st.status == "finished"}
            if not all_finished:
                continue
            live_rows = _servable(live, game, league, cutoff)
            state_path = archive_paths(Path(archive_dir), game, league)[2]
            merged, state_error = _merged_ids(state_path)
            covered_by_copy: dict[int, tuple[str, str]] = {}  # sid -> (created_at, copy name)
            for name, conn in copy_conns:
                for sid, created in _servable(conn, game, league, cutoff).items():
                    covered_by_copy.setdefault(sid, (created, name))
            safe = (merged or set()) | set(covered_by_copy)
            at_risk = sorted(((sid, c) for sid, c in live_rows.items() if sid not in safe), key=lambda x: x[0])
            lost = sorted(sid for sid in all_finished if sid not in live_rows and sid not in safe)
            deletions = [(sid, c, retention_delete_at(c, retention_days, cleanup_hour_utc)) for sid, c in at_risk]
            earliest = min((d for _, _, d in deletions), default=None)
            if earliest is not None and (overall is None or earliest < overall):
                overall = earliest
            through = None
            if covered_by_copy:
                sid, (created, name) = max(covered_by_copy.items(), key=lambda kv: (kv[1][0], kv[0]))
                through = {"snapshot_id": sid, "created_at": created, "copy": name}
            entry = {
                "game": game, "league": league, "live_snapshots_with_rows": len(live_rows),
                "archive_merged": None if merged is None else len(merged),
                "backup_covers_through": through, "at_risk_count": len(at_risk),
                "at_risk": [{"snapshot_id": sid, "created_at": c, "retention_delete_at": _iso(d)}
                            for sid, c, d in deletions[:MAX_LISTED]],
                "earliest_loss_at": None if earliest is None else _iso(earliest),
                "lost_count": len(lost), "lost_snapshot_ids": lost[:MAX_LISTED],
            }
            if state_error:
                entry["archive_state_error"] = state_error
            leagues_out.append(entry)
    finally:
        live.close()
        for _, conn in copy_conns:
            conn.close()
    return {"computed_at": _iso(now), "earliest_loss_at": None if overall is None else _iso(overall),
            "copies": copy_report, "leagues": leagues_out}
