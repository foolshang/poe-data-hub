"""Unit tests for src/utils/job_lock.py's cross-process lock -- added
2026-07-15 after a manual meta-job retry collided with the already-running
scheduled meta cron, both hammering poe.ninja independently for hours (see
CLAUDE.md's "Deploy gotcha"-adjacent incident writeup)."""
from __future__ import annotations

import json
import os

import pytest

from src.utils import job_lock


def test_acquire_then_release_leaves_no_lock_file(tmp_path):
    with job_lock.acquire("meta", lock_dir=tmp_path):
        assert (tmp_path / "meta.lock").exists()
    assert not (tmp_path / "meta.lock").exists()


def test_second_acquire_while_held_raises_job_already_running(tmp_path):
    with job_lock.acquire("meta", lock_dir=tmp_path):
        with pytest.raises(job_lock.JobAlreadyRunning):
            with job_lock.acquire("meta", lock_dir=tmp_path):
                pass  # pragma: no cover -- must not be reached


def test_job_already_running_carries_holder_pid(tmp_path):
    with job_lock.acquire("meta", lock_dir=tmp_path):
        try:
            with job_lock.acquire("meta", lock_dir=tmp_path):
                pass  # pragma: no cover
        except job_lock.JobAlreadyRunning as e:
            assert e.holder.pid == os.getpid()
        else:
            pytest.fail("expected JobAlreadyRunning")


def test_lock_released_even_if_body_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with job_lock.acquire("meta", lock_dir=tmp_path):
            raise RuntimeError("boom")
    assert not (tmp_path / "meta.lock").exists()


def test_stale_lock_from_dead_pid_is_silently_reclaimed(tmp_path):
    # A pid outside any real range on this machine -- simulates a lock file
    # left behind by a crashed process that could never clean up after itself.
    dead_pid = 999999999
    lock_path = tmp_path / "meta.lock"
    lock_path.write_text(json.dumps({"pid": dead_pid, "started_at": "2020-01-01T00:00:00Z"}), encoding="utf-8")

    with job_lock.acquire("meta", lock_dir=tmp_path):
        info = json.loads(lock_path.read_text(encoding="utf-8"))
        assert info["pid"] == os.getpid()


def test_read_lock_returns_none_when_unlocked(tmp_path):
    assert job_lock.read_lock("meta", lock_dir=tmp_path) is None


def test_read_lock_returns_none_for_stale_dead_pid_lock(tmp_path):
    lock_path = tmp_path / "meta.lock"
    lock_path.write_text(json.dumps({"pid": 999999999, "started_at": "2020-01-01T00:00:00Z"}), encoding="utf-8")
    assert job_lock.read_lock("meta", lock_dir=tmp_path) is None


def test_read_lock_returns_holder_info_while_held(tmp_path):
    with job_lock.acquire("meta", lock_dir=tmp_path):
        info = job_lock.read_lock("meta", lock_dir=tmp_path)
        assert info is not None
        assert info.pid == os.getpid()


def test_different_job_names_do_not_block_each_other(tmp_path):
    with job_lock.acquire("meta", lock_dir=tmp_path):
        with job_lock.acquire("prices", lock_dir=tmp_path):
            pass  # must not raise
