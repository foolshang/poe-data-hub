"""One league's archive export on top of the streaming builder and the accumulating skills state.

Three files per (game, league) in `archive_dir`:
- `{game}-{slug}.json` -- the archive other apps already read: `mod_counts`, `skill_mod_counts`, `passives`,
  recomputed in full from the legacy tables (never cleaned by retention) on every run, rewritten atomically;
- `{game}-{slug}.skills.json` -- the new `skills` dimension, rendered from the state (see `archive_skills.py`);
- `{game}-{slug}.skills.state.sqlite3` -- the accumulation itself, the only copy of snapshots whose raw rows retention
  has deleted (`.bak` beside it is refreshed before every merge that changes it).

`rebuild=True` ignores whatever exists: the three files (and the `.bak`) are renamed `*.legacy` (never deleted, never
overwritten -- a timestamp is added if a `.legacy` already exists) and the skills state starts from nothing, merged
from the sources given.

Read-only on every database (`mode=ro`); the only thing written besides the files above is the `.tmp` of an atomic write.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from src.processing.archive_skills import (
    ArchiveStateError, LegacyArchiveError, MergeReport, Source, merge_snapshots, open_state, render_skills_file,
)
from src.processing.archive_stream import build_league_archive_to_file
from src.storage.publisher import sanitize_league_filename

log = logging.getLogger(__name__)

DEFAULT_ARCHIVE_DIR = Path("data/archive")


@dataclass
class ExportResult:
    base_path: Path
    skills_path: Path
    state_path: Path
    base_bytes: int = 0
    skills_bytes: int = 0
    merge: MergeReport = field(default_factory=MergeReport)
    moved_to_legacy: list[Path] = field(default_factory=list)
    publish_errors: list[str] = field(default_factory=list)


def archive_paths(archive_dir: Path, game: str, league: str) -> tuple[Path, Path, Path]:
    stem = f"{game}-{sanitize_league_filename(league)}"
    return (archive_dir / f"{stem}.json", archive_dir / f"{stem}.skills.json",
            archive_dir / f"{stem}.skills.state.sqlite3")


def _move_to_legacy(path: Path) -> Optional[Path]:
    """`<name>` -> `<name>.legacy` (or `<name>.legacy.<utc stamp>` when that exists). Never overwrites, never deletes."""
    if not path.exists():
        return None
    target = path.with_name(path.name + ".legacy")
    if target.exists():
        target = path.with_name(f"{path.name}.legacy.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        if target.exists():
            raise ArchiveStateError(f"cannot preserve {path}: {target} exists too")
    os.rename(path, target)
    return target


def export_league_archive_streaming(
    db_path: Path, game: str, league: str, archive_dir: Path = DEFAULT_ARCHIVE_DIR, publisher: Any = None,
    extra_sources: Sequence[Path] = (), rebuild: bool = False, resume_max_age_hours: float = 48.0,
    now: Optional[datetime] = None,
) -> ExportResult:
    """See the module docstring. Raises `ArchiveStateError` (and subclasses) rather than touching anything when the
    existing files are not in a state a merge may build on; publish failures never raise (the VM files are the durable
    copy) but are listed in `ExportResult.publish_errors`."""
    archive_dir = Path(archive_dir)
    base_path, skills_path, state_path = archive_paths(archive_dir, game, league)
    result = ExportResult(base_path, skills_path, state_path)
    sources = [Source("live", Path(db_path))] + [Source(f"copy{i + 1}", Path(p)) for i, p in enumerate(extra_sources)]
    for s in sources:
        if not s.path.exists():
            raise ArchiveStateError(f"source {s.name} {s.path} does not exist")

    if rebuild:
        for p in (base_path, skills_path, state_path, state_path.with_name(state_path.name + ".bak")):
            moved = _move_to_legacy(p)
            if moved is not None:
                result.moved_to_legacy.append(moved)
                log.warning("archive: rebuild moved %s -> %s", p, moved)
    elif not state_path.exists():
        if skills_path.exists():
            raise LegacyArchiveError(
                f"{skills_path} exists but {state_path} does not: nothing records which snapshots the file already "
                "holds, so merging could count them twice. Re-run with --rebuild-archive (the old files are kept as .legacy)."
            )
        # first export on the accumulating format: keep whatever base archive the older builder left behind
        moved = _move_to_legacy(base_path)
        if moved is not None:
            result.moved_to_legacy.append(moved)
            log.warning("archive: kept the pre-existing %s as %s", base_path, moved)

    # 1. the three legacy dimensions, from the legacy tables: a full recompute is exact (retention never touches them)
    info = build_league_archive_to_file(db_path, game, league, base_path, include_skills=False)
    result.base_bytes = info["bytes"]

    # 2. the skills dimension: accumulate finished snapshots into the state, then render
    state = open_state(state_path, game, league, create=not state_path.exists())
    try:
        result.merge = merge_snapshots(state, sources, game, league, resume_max_age_hours, now)
        rendered = render_skills_file(state, game, league, skills_path)
    finally:
        state.close()
    result.skills_bytes = rendered["bytes"]
    m = result.merge
    log.info(
        "archive: %s/%s merged %d new snapshot(s), %d already merged, %d unavailable, %d stale running, base %.1fMB, skills %.1fMB",
        game, league, len(m.merged), m.already_merged, len(m.unavailable), len(m.stale_running),
        result.base_bytes / 1e6, result.skills_bytes / 1e6,
    )
    if m.unavailable:
        log.error("archive: %s/%s snapshot(s) %s are finished but no source holds their rows -- lost unless a copy is added",
                  game, league, m.unavailable)

    # 3. publish (best effort: the files on the VM are the durable ones)
    if publisher is not None:
        slug = sanitize_league_filename(league)
        for path, blob, gz in ((base_path, f"archive/{game}-{slug}.json", False),
                               (skills_path, f"archive/{game}-{slug}.skills.json", True)):
            try:
                publisher.publish_file(path, blob, cache_control="public, max-age=3600", gzip_upload=gz)
            except Exception as e:
                msg = f"{blob}: {e}"
                result.publish_errors.append(msg)
                log.warning("archive: could not publish %s to Firebase (kept on the VM only): %s", blob, e)
    return result
