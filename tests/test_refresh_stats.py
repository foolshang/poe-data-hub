"""Unit tests for tools/refresh_stats.py."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from tools import refresh_stats

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ggg_trade2_stats.json"


@pytest.fixture
def real_stats() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def test_validate_accepts_real_sample(real_stats):
    total = refresh_stats.validate(real_stats)
    assert total >= refresh_stats.MIN_TOTAL_ENTRIES


def test_validate_rejects_non_dict():
    with pytest.raises(ValueError, match="unexpected shape"):
        refresh_stats.validate(["not", "a", "dict"])


def test_validate_rejects_missing_result_key():
    with pytest.raises(ValueError, match="unexpected shape"):
        refresh_stats.validate({"nope": []})


def test_validate_rejects_empty_result():
    with pytest.raises(ValueError, match="unexpected shape"):
        refresh_stats.validate({"result": []})


def test_validate_rejects_malformed_group():
    with pytest.raises(ValueError, match="malformed group"):
        refresh_stats.validate({"result": [{"id": "explicit"}]})  # missing "entries"


def test_validate_rejects_suspiciously_low_entry_count():
    tiny = {"result": [{"id": "explicit", "entries": [{"id": "x", "text": "y"}]}]}
    with pytest.raises(ValueError, match="suspiciously low"):
        refresh_stats.validate(tiny)


def test_run_uploads_on_success(real_stats):
    mock_resp = MagicMock()
    mock_resp.json.return_value = real_stats
    mock_resp.raise_for_status.return_value = None

    with patch("requests.get", return_value=mock_resp), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        mock_publisher = MockPublisher.return_value
        code = refresh_stats.run()

    assert code == 0
    mock_publisher.publish.assert_called_once()
    args, kwargs = mock_publisher.publish.call_args
    envelope = args[0]
    assert envelope["data"] == real_stats
    assert "fetched_at" in envelope
    assert args[1] == refresh_stats.BLOB_PATH


def test_run_does_not_upload_on_fetch_failure():
    with patch("requests.get", side_effect=requests.ConnectionError("boom")), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        code = refresh_stats.run()

    assert code == 1
    MockPublisher.return_value.publish.assert_not_called()


def test_run_does_not_upload_on_validation_failure():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"result": []}
    mock_resp.raise_for_status.return_value = None

    with patch("requests.get", return_value=mock_resp), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        code = refresh_stats.run()

    assert code == 1
    MockPublisher.return_value.publish.assert_not_called()


def test_run_returns_error_when_publish_raises(real_stats):
    mock_resp = MagicMock()
    mock_resp.json.return_value = real_stats
    mock_resp.raise_for_status.return_value = None

    with patch("requests.get", return_value=mock_resp), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        MockPublisher.return_value.publish.side_effect = RuntimeError("verification MISMATCH")
        code = refresh_stats.run()

    assert code == 1
