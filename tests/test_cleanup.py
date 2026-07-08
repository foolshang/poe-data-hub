from __future__ import annotations

import time
from pathlib import Path

from src.main import _cleanup_old_local_files


def test_cleanup_old_local_files_deletes_only_old_files(tmp_path: Path):
    old_file = tmp_path / "old.json"
    new_file = tmp_path / "new.json"
    old_file.write_text("{}")
    new_file.write_text("{}")

    old_time = time.time() - 40 * 86400
    import os
    os.utime(old_file, (old_time, old_time))

    deleted = _cleanup_old_local_files(tmp_path, older_than_days=30)

    assert deleted == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_cleanup_old_local_files_missing_directory_returns_zero(tmp_path: Path):
    assert _cleanup_old_local_files(tmp_path / "does-not-exist", older_than_days=30) == 0


def test_cleanup_old_local_files_ignores_subdirectories(tmp_path: Path):
    subdir = tmp_path / "sub"
    subdir.mkdir()
    old_time = time.time() - 40 * 86400
    import os
    os.utime(subdir, (old_time, old_time))

    deleted = _cleanup_old_local_files(tmp_path, older_than_days=30)
    assert deleted == 0
    assert subdir.exists()
