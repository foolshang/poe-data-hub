"""Unit tests for src/storage/publisher.py's post-publish verification
(added after a user-reported cache-vs-real-failure ambiguity: publish()
must fail loudly, not silently, if the readback doesn't match what was
uploaded)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.storage.publisher import PricesPublisher


@pytest.fixture
def publisher(tmp_path) -> PricesPublisher:
    return PricesPublisher(bucket_name="test-bucket", local_cache_dir=tmp_path)


def _mock_client(readback_text):
    """A fake google.cloud.storage.Client whose blob.download_as_text()
    returns `readback_text` regardless of upload_from_string content."""
    blob = MagicMock()
    blob.download_as_text.return_value = readback_text
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket
    return client, blob, bucket


def test_publish_succeeds_when_readback_matches(publisher: PricesPublisher):
    data = {"a": 1}
    import json
    expected_payload = json.dumps(data, ensure_ascii=False, indent=2)
    client, blob, bucket = _mock_client(expected_payload)
    publisher._client = client

    publisher.publish(data, "poe2/meta/latest.json", cache_control="no-cache")

    blob.upload_from_string.assert_called_once()


def test_publish_raises_on_content_mismatch(publisher: PricesPublisher):
    data = {"a": 1}
    client, blob, bucket = _mock_client('{"a": 2}')  # different content
    publisher._client = client

    with pytest.raises(RuntimeError, match="MISMATCH"):
        publisher.publish(data, "poe2/meta/latest.json", cache_control="no-cache")


def test_publish_raises_when_readback_fails(publisher: PricesPublisher):
    data = {"a": 1}
    client, blob, bucket = _mock_client(None)
    blob.download_as_text.side_effect = Exception("network blip")
    publisher._client = client

    with pytest.raises(RuntimeError, match="could not read back"):
        publisher.publish(data, "poe2/meta/latest.json", cache_control="no-cache")
