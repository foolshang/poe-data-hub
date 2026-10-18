from src.utils.log_rotation import append_text, rotate_if_needed


def test_rotates_when_over_limit_and_keeps_only_n_backups(tmp_path):
    log = tmp_path / "x.log"
    for i in range(6):
        append_text(log, f"line-{i}" + "x" * 20 + "\n", max_bytes=30, backups=2)

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["x.log", "x.log.1", "x.log.2"]  # never a .3
    assert "line-5" in log.read_text()                 # newest write is in the live file


def test_no_rotation_below_limit_or_when_file_missing(tmp_path):
    log = tmp_path / "y.log"
    assert rotate_if_needed(log, max_bytes=10) is False
    append_text(log, "short\n", max_bytes=1000)
    assert rotate_if_needed(log, max_bytes=1000) is False
    assert [p.name for p in tmp_path.iterdir()] == ["y.log"]


def test_oldest_backup_is_overwritten_not_accumulated(tmp_path):
    log = tmp_path / "z.log"
    (tmp_path / "z.log.1").write_text("old1")
    (tmp_path / "z.log.2").write_text("old2")
    log.write_text("A" * 50)
    assert rotate_if_needed(log, max_bytes=10, backups=2) is True
    assert (tmp_path / "z.log.1").read_text() == "A" * 50
    assert (tmp_path / "z.log.2").read_text() == "old1"
    assert not log.exists()
