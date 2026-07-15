"""Unit tests for tools/refresh_stats.py (phase 10: both games, independent
per-target fetch/validate/publish)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from tools import refresh_stats

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ggg_trade2_stats.json"
POE1_SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "poe1_trade_stats.json"


@pytest.fixture
def real_stats() -> dict:
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))


def test_validate_accepts_real_poe1_sample():
    poe1_stats = json.loads(POE1_SAMPLE_PATH.read_text(encoding="utf-8"))
    total = refresh_stats.validate(poe1_stats)
    assert total >= refresh_stats.MIN_TOTAL_ENTRIES


def _resp(data):
    mock_resp = MagicMock()
    mock_resp.json.return_value = data
    mock_resp.raise_for_status.return_value = None
    return mock_resp


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


def test_targets_cover_both_games_with_distinct_urls_and_paths():
    games = {t.game for t in refresh_stats.TARGETS}
    assert games == {"poe1", "poe2"}
    urls = {t.stats_url for t in refresh_stats.TARGETS}
    assert len(urls) == 2  # trade2 vs trade -- must not collide
    paths = {t.blob_path for t in refresh_stats.TARGETS}
    assert len(paths) == 2  # separate Firebase files -- must not collide


def test_run_uploads_both_games_on_success(real_stats):
    with patch("requests.get", side_effect=[_resp(real_stats), _resp(real_stats)]), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        mock_publisher = MockPublisher.return_value
        code = refresh_stats.run()

    assert code == 0
    assert mock_publisher.publish.call_count == 2
    published_paths = {call.args[1] for call in mock_publisher.publish.call_args_list}
    assert published_paths == {t.blob_path for t in refresh_stats.TARGETS}
    for call in mock_publisher.publish.call_args_list:
        envelope = call.args[0]
        assert envelope["data"] == real_stats
        assert "fetched_at" in envelope


def test_run_one_game_fetch_failure_does_not_block_the_other(real_stats):
    # poe2 (first target) fails, poe1 (second) succeeds -- still uploads poe1.
    with patch("requests.get", side_effect=[requests.ConnectionError("boom"), _resp(real_stats)]), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        mock_publisher = MockPublisher.return_value
        code = refresh_stats.run()

    assert code == 1  # overall failure reported...
    mock_publisher.publish.assert_called_once()  # ...but poe1 still got uploaded
    assert mock_publisher.publish.call_args.args[1] == "internal/stats_cache_poe1.json"


def test_run_does_not_upload_on_validation_failure():
    with patch("requests.get", side_effect=[_resp({"result": []}), _resp({"result": []})]), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        code = refresh_stats.run()

    assert code == 1
    MockPublisher.return_value.publish.assert_not_called()


def test_run_returns_error_when_publish_raises(real_stats):
    with patch("requests.get", side_effect=[_resp(real_stats), _resp(real_stats)]), \
         patch("tools.refresh_stats.PricesPublisher") as MockPublisher:
        MockPublisher.return_value.publish.side_effect = RuntimeError("verification MISMATCH")
        code = refresh_stats.run()

    assert code == 1
