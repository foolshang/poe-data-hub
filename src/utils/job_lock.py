"""Cross-process lock for jobs that must never run twice concurrently.

The daemon (`--serve`, running jobs on threads inside one long-lived
process) and the manual one-shot CLI (`--run-meta-once` etc.) are separate
OS processes, so an in-process lock can't coordinate between them. This is
filesystem-based instead: a lock file under `data/locks/` holding the
holder's pid + start time.

Added 2026-07-15 after the scheduled 2am-Thailand meta cron and a manually
launched `--run-meta-once` retry ran concurrently, both hammering poe.ninja
independently and tripping its server-side rate limiting for hours (see
CLAUDE.md). `run_meta_once` now acquires this lock before doing any work;
a second concurrent attempt (from either the scheduler or a human on the
CLI) skips cleanly instead of colliding.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

DEFAULT_LOCK_DIR = Path("data/locks")


@dataclass(frozen=True)
class LockInfo:
    pid: int
    started_at: str  # ISO8601 UTC


class JobAlreadyRunning(RuntimeError):
    def __init__(self, job_name: str, holder: LockInfo):
        super().__init__(f"{job_name} job already running (pid={holder.pid}, started_at={holder.started_at})")
        self.job_name = job_name
        self.holder = holder


def _lock_path(job_name: str, lock_dir: Path) -> Path:
    return lock_dir / f"{job_name}.lock"


def _pid_alive(pid: int) -> bool:
    if os.name != "posix":
        # Windows dev machines only (prod is always the Linux VM): NEVER use
        # os.kill(pid, 0) here. On Windows, any signal value other than
        # CTRL_C_EVENT/CTRL_BREAK_EVENT is implemented as TerminateProcess(),
        # so a signal of 0 doesn't "check" the pid -- it kills it. Query the
        # handle instead, which never terminates anything.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else -- still alive
    except OSError:
        return False
    return True


def read_lock(job_name: str, lock_dir: Optional[Path] = None) -> Optional[LockInfo]:
    """Current lock holder for `job_name`, or None if unlocked -- including
    when a lock file exists but its pid is no longer alive (a crashed
    process can never clean up its own lock file, so a dead pid must be
    treated as unlocked rather than jamming the job forever)."""
    lock_dir = lock_dir if lock_dir is not None else DEFAULT_LOCK_DIR
    path = _lock_path(job_name, lock_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        info = LockInfo(pid=raw["pid"], started_at=raw["started_at"])
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    if not _pid_alive(info.pid):
        return None
    return info


@contextmanager
def acquire(job_name: str, lock_dir: Optional[Path] = None) -> Iterator[None]:
    """Raises JobAlreadyRunning if another live process already holds this
    job's lock. Both the scheduler and the manual CLI call the same
    top-level job function (e.g. `run_meta_once`), so wrapping that
    function's body in this lock covers both callers automatically."""
    lock_dir = lock_dir if lock_dir is not None else DEFAULT_LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = _lock_path(job_name, lock_dir)

    fd = None
    for _attempt in range(2):  # 2nd attempt only after clearing a stale (dead-pid) lock
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            existing = read_lock(job_name, lock_dir)
            if existing is not None:
                raise JobAlreadyRunning(job_name, existing)
            path.unlink(missing_ok=True)  # stale (holder pid is dead) -- clear and retry
    if fd is None:
        existing = read_lock(job_name, lock_dir)
        if existing is not None:
            raise JobAlreadyRunning(job_name, existing)
        raise RuntimeError(f"could not acquire {job_name} lock at {path} (repeated contention)")

    info = LockInfo(pid=os.getpid(), started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps({"pid": info.pid, "started_at": info.started_at}))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
