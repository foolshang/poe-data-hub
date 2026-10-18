"""Size-based rotation for the append-only JSONL logs under data/
(fetch.log, unmapped_mods*.log).

`hub.log` already rotates daily via logging's TimedRotatingFileHandler; these
files are written with a bare `open("a")` and never rotated, which by
2026-10-18 had grown `unmapped_mods_poe1.log` to 807MB and `fetch.log` to
189MB on a 29GB disk. Keeps `backups` older files (`name.1` newest ..
`name.<backups>` oldest); the oldest is overwritten on the next rotation.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_BACKUPS = 2

# Jobs run on scheduler threads of one process, so a process-wide lock is
# enough; a second process (a manual CLI run) racing a rotation only loses
# the rename, which is swallowed below.
_lock = threading.Lock()


def rotate_if_needed(path: Path, max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS) -> bool:
    """Rotates `path` when it has reached `max_bytes`. Returns True if it did."""
    try:
        if path.stat().st_size < max_bytes:
            return False
    except FileNotFoundError:
        return False
    for i in range(backups, 0, -1):
        src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
        dst = path.with_name(f"{path.name}.{i}")
        try:
            os.replace(src, dst)
        except FileNotFoundError:
            pass
    return True


def append_text(path: Path, text: str, max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS) -> None:
    with _lock:
        rotate_if_needed(path, max_bytes, backups)
        with path.open("a", encoding="utf-8") as f:
            f.write(text)
