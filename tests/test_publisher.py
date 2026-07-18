"""Unit tests for src/storage/publisher.py's post-publish verification
(added after a user-reported cache-vs-real-failure ambiguity: publish()
must fail loudly, not silently, if the readback doesn't match what was
uploaded)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.storage.publisher import (
    PricesPublisher,
    build_index_schema,
    build_mtx_schema,
    build_news_schema,
    build_prices_schema,
    sanitize_league_filename,
)


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


def test_build_prices_schema_without_scout_omits_scout_key():
    data = build_prices_schema(
        league="Mirage",
        ninja_entries=[{"category": "Currency", "name": "Chaos Orb", "value": 1, "value_currency": "chaos", "divine_value": 0.001}],
        ninja_ok=True,
        ninja_bucket_of=lambda c: "currency",
    )
    assert data["sources"] == {"ninja": "ok"}
    assert "scout" not in data["sources"]
    assert len(data["currency"]) == 1
    assert data["sample"] == {"per_category": {}}


def test_build_prices_schema_includes_per_category_ninja_only_for_poe1():
    data = build_prices_schema(
        league="Mirage",
        ninja_entries=[{"category": "Currency", "name": "Chaos Orb", "value": 1, "value_currency": "chaos", "divine_value": 0.001}],
        ninja_ok=True,
        ninja_bucket_of=lambda c: "currency",
        ninja_per_category={"Currency": {"ok": True, "count": 1}, "UniqueWeapon": {"ok": False, "count": 0}},
        scout_per_category={"Currency": {"ok": True, "count": 1}},  # would be ignored: scout not used for poe1
    )
    assert data["sample"]["per_category"]["ninja"]["Currency"] == {"ok": True, "count": 1}
    assert data["sample"]["per_category"]["ninja"]["UniqueWeapon"] == {"ok": False, "count": 0}
    assert "scout" not in data["sample"]["per_category"]


def test_build_prices_schema_includes_per_category_both_sources_for_poe2():
    data = build_prices_schema(
        league="Runes of Aldur",
        ninja_entries=[],
        ninja_ok=True,
        ninja_bucket_of=lambda c: "currency",
        ninja_per_category={"Currency": {"ok": True, "count": 5}},
        scout_entries=[],
        scout_ok=True,
        scout_bucket_of=lambda c: "currency",
        scout_per_category={"Currency": {"ok": True, "count": 3}},
    )
    assert data["sample"]["per_category"] == {
        "ninja": {"Currency": {"ok": True, "count": 5}},
        "scout": {"Currency": {"ok": True, "count": 3}},
    }


def test_build_prices_schema_omits_per_category_when_source_never_ran():
    data = build_prices_schema(
        league="Mirage",
        ninja_entries=[],
        ninja_ok=False,
        ninja_bucket_of=lambda c: "currency",
    )
    assert data["sample"]["per_category"] == {}


def test_build_prices_schema_with_scout_includes_scout_key():
    data = build_prices_schema(
        league="Runes of Aldur",
        ninja_entries=[],
        ninja_ok=True,
        ninja_bucket_of=lambda c: "currency",
        scout_entries=[{"category": "UniqueWeapon", "name": "Foo", "value": 1, "value_currency": "exalted", "divine_value": 0.01}],
        scout_ok=True,
        scout_bucket_of=lambda c: "items",
    )
    assert data["sources"] == {"ninja": "ok", "scout": "ok"}
    assert len(data["items"]) == 1


def test_build_prices_schema_scout_error_uses_stale_previous():
    previous = {
        "currency": [],
        "items": [{"category": "UniqueWeapon", "name": "Foo", "source": "scout", "value": 1, "value_currency": "exalted", "divine_value": 0.01}],
    }
    data = build_prices_schema(
        league="Runes of Aldur",
        ninja_entries=[],
        ninja_ok=True,
        ninja_bucket_of=lambda c: "currency",
        scout_entries=[],
        scout_ok=False,
        scout_bucket_of=lambda c: "items",
        previous=previous,
    )
    assert data["sources"]["scout"] == "error"
    assert len(data["items"]) == 1
    assert data["items"][0]["stale"] is True


def test_sanitize_league_filename():
    assert sanitize_league_filename("Return of the Ancients") == "return-of-the-ancients"
    assert sanitize_league_filename("HC Runes of Aldur") == "hc-runes-of-aldur"
    assert sanitize_league_filename("Mirage") == "mirage"
    assert sanitize_league_filename("  ¡Weird!!  ") == "weird"


def test_build_index_schema_with_events():
    data = build_index_schema(
        main_league="Runes of Aldur",
        main_path="poe2/prices/latest.json",
        main_generated_at="2026-07-11T00:00:00Z",
        events=[{"league": "Some Event", "path": "poe2/prices/events/some-event.json", "generated_at": "2026-07-11T00:00:00Z"}],
    )
    assert data["main"]["league"] == "Runes of Aldur"
    assert len(data["events"]) == 1
    assert data["events"][0]["league"] == "Some Event"
    assert data["hardcore"] is None  # default: no hardcore variant found


def test_build_index_schema_with_hardcore():
    data = build_index_schema(
        main_league="Runes of Aldur",
        main_path="poe2/prices/latest.json",
        main_generated_at="2026-07-11T00:00:00Z",
        events=[],
        hardcore={"league": "HC Runes of Aldur", "path": "poe2/prices/hardcore/latest.json", "generated_at": "2026-07-11T00:00:00Z"},
    )
    assert data["hardcore"]["league"] == "HC Runes of Aldur"
    assert data["hardcore"]["path"] == "poe2/prices/hardcore/latest.json"


def test_delete_blobs_not_in_deletes_only_unlisted(publisher: PricesPublisher):
    class _FakeListBlob:
        def __init__(self, name):
            self.name = name
            self.deleted = False

        def delete(self):
            self.deleted = True

    blobs = [
        _FakeListBlob("poe2/prices/events/mirage-event.json"),
        _FakeListBlob("poe2/prices/events/closed-event.json"),
    ]
    bucket = MagicMock()
    bucket.list_blobs.return_value = blobs
    client = MagicMock()
    client.bucket.return_value = bucket
    publisher._client = client

    deleted = publisher.delete_blobs_not_in("poe2/prices/events/", keep_names=frozenset({"mirage-event.json"}))

    assert deleted == 1
    assert blobs[0].deleted is False
    assert blobs[1].deleted is True


def test_build_index_schema_no_events():
    data = build_index_schema(
        main_league="Mirage", main_path="poe1/prices/latest.json", main_generated_at="2026-07-11T00:00:00Z", events=[],
    )
    assert data["events"] == []


def test_build_news_schema_ok():
    entries = [{"id": "forum-1", "title": "Hello"}]
    data = build_news_schema(entries, ok=True)
    assert data["sources"] == {"forum": "ok"}
    assert data["entries"] == entries
    assert data["schema_version"] == 2


def test_build_news_schema_error_reuses_previous_entries():
    previous = {"entries": [{"id": "forum-1", "title": "Old"}]}
    data = build_news_schema([], ok=False, previous=previous)
    assert data["sources"] == {"forum": "error"}
    assert data["entries"] == previous["entries"]


def test_build_news_schema_error_no_previous_publishes_empty():
    data = build_news_schema([], ok=False, previous=None)
    assert data["sources"] == {"forum": "error"}
    assert data["entries"] == []


def test_build_mtx_schema_new_deal_gets_first_seen_at_now():
    deals = [{"item_name": "Premium Quad Stash Tab", "link": "x"}]
    data = build_mtx_schema(deals, ok=True, previous=None)
    assert data["sources"] == {"ggg_shop": "ok"}
    assert data["deals"][0]["first_seen_at"] == data["generated_at"]


def test_build_mtx_schema_existing_deal_keeps_original_first_seen_at():
    previous = {
        "generated_at": "2026-07-10T00:00:00Z",
        "deals": [{"item_name": "Premium Quad Stash Tab", "link": "x", "first_seen_at": "2026-07-09T00:00:00Z"}],
    }
    deals = [{"item_name": "Premium Quad Stash Tab", "link": "x"}]
    data = build_mtx_schema(deals, ok=True, previous=previous)
    assert data["deals"][0]["first_seen_at"] == "2026-07-09T00:00:00Z"


def test_build_mtx_schema_mixed_new_and_existing_deals():
    previous = {"deals": [{"item_name": "A", "first_seen_at": "2026-07-01T00:00:00Z"}]}
    deals = [{"item_name": "A"}, {"item_name": "B"}]
    data = build_mtx_schema(deals, ok=True, previous=previous)
    by_name = {d["item_name"]: d for d in data["deals"]}
    assert by_name["A"]["first_seen_at"] == "2026-07-01T00:00:00Z"
    assert by_name["B"]["first_seen_at"] == data["generated_at"]


def test_build_mtx_schema_error_reuses_previous_deals_verbatim():
    previous = {"deals": [{"item_name": "A", "first_seen_at": "2026-07-01T00:00:00Z"}]}
    data = build_mtx_schema([], ok=False, previous=previous)
    assert data["sources"] == {"ggg_shop": "error"}
    assert data["deals"] == previous["deals"]
