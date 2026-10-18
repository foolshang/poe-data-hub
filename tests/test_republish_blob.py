import json
from unittest.mock import MagicMock, patch

from tools import republish_blob


def _run(tmp_path, args, current, payload=None):
    backup = tmp_path / "backup.json"
    backup.write_text(json.dumps(payload if payload is not None else {"generated_at": "t0", "mods": [1, 2]}), encoding="utf-8")
    publisher = MagicMock()
    publisher.fetch_previous.return_value = current
    with patch("tools.republish_blob.PricesPublisher", return_value=publisher), \
         patch("tools.republish_blob.load_config", return_value={"firebase": {"bucket": "b"}}):
        code = republish_blob.main([str(backup), "poe1/meta/latest.json", *args])
    return code, publisher


def test_dry_run_publishes_nothing(tmp_path):
    code, publisher = _run(tmp_path, [], current={"generated_at": "t1"})
    assert code == 0
    publisher.publish.assert_not_called()


def test_apply_republishes_the_backup_with_the_meta_cache_header(tmp_path):
    code, publisher = _run(tmp_path, ["--apply"], current={"generated_at": "t1"})
    assert code == 0
    (data, path), kwargs = publisher.publish.call_args
    assert data == {"generated_at": "t0", "mods": [1, 2]} and path == "poe1/meta/latest.json"
    assert kwargs["cache_control"] == "public, max-age=3600"


def test_refuses_a_blob_path_that_does_not_exist_yet(tmp_path):
    code, publisher = _run(tmp_path, ["--apply"], current=None)
    assert code == 2
    publisher.publish.assert_not_called()


def test_allow_new_overrides_the_missing_blob_guard(tmp_path):
    code, publisher = _run(tmp_path, ["--apply", "--allow-new"], current=None)
    assert code == 0
    publisher.publish.assert_called_once()


def test_refuses_a_backup_that_is_not_a_json_object(tmp_path):
    code, publisher = _run(tmp_path, ["--apply"], current={"x": 1}, payload=[1, 2, 3])
    assert code == 2
    publisher.publish.assert_not_called()
