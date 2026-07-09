"""Unit tests for src/processing/normalizer.py against the real trade2 stats sample.

Sample was captured live from https://www.pathofexile.com/api/trade2/data/stats
on 2026-07-08 (data/samples/ggg_trade2_stats.json) per the "test once, save
sample" rule in spec section 11 — these tests never hit the network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.processing.normalizer import ModNormalizer

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "ggg_trade2_stats.json"


class _FakeHttpClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def get_json(self, url):
        self.calls += 1
        return self._payload


@pytest.fixture
def normalizer(tmp_path) -> ModNormalizer:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    n = ModNormalizer(
        http_client=http,
        cache_path=tmp_path / "stats_cache.json",
        unmapped_log_path=tmp_path / "unmapped_mods.log",
    )
    n.load()
    return n


def test_simple_explicit_life_mod(normalizer: ModNormalizer):
    result = normalizer.normalize("+87 to maximum Life", "explicit")
    assert len(result) == 1
    m = result[0]
    assert m.stat_id == "explicit.stat_3299347043"
    assert m.mod_kind == "explicit"
    assert m.values == [87.0]


def test_percent_resistance_mod(normalizer: ModNormalizer):
    result = normalizer.normalize("+23% to Cold Resistance", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_4220027924"
    assert result[0].values == [23.0]


def test_two_value_damage_range_mod(normalizer: ModNormalizer):
    result = normalizer.normalize("Adds 5 to 10 Physical Damage to Attacks", "explicit")
    assert len(result) == 1
    m = result[0]
    assert m.stat_id == "explicit.stat_3032590688"
    assert m.values == [5.0, 10.0]


def test_literal_plus_sign_baked_into_template(normalizer: ModNormalizer):
    # "+#% Monster Elemental Resistances" bakes a literal "+" into the
    # template itself (always-positive map mod, no "-" counterpart exists),
    # so naive sign-swallowing skeletonization would miss it.
    result = normalizer.normalize("+15% Monster Elemental Resistances", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_1054098949"
    assert result[0].values == [15.0]


def test_implicit_mod_kind_is_respected(normalizer: ModNormalizer):
    # Same display text can exist under a different stat_id per mod_kind.
    result = normalizer.normalize("+18% increased Rarity of Items found", "implicit")
    assert len(result) == 1
    assert result[0].stat_id == "implicit.stat_3917489142"
    assert result[0].mod_kind == "implicit"


def test_wrong_mod_kind_does_not_match(normalizer: ModNormalizer):
    # "#% reduced Elemental Ailment Duration on you" exists only under
    # "implicit" in the sample table, not under "fractured" -> unmapped.
    result = normalizer.normalize("20% reduced Elemental Ailment Duration on you", "fractured")
    assert result == []


def test_unmapped_mod_is_logged_not_raised(normalizer: ModNormalizer, tmp_path):
    result = normalizer.normalize("This is definitely not a real mod #", "explicit")
    assert result == []
    log_path = tmp_path / "unmapped_mods.log"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["mod_kind"] == "explicit"
    assert "not a real mod" in entry["text"]


def test_stale_cache_used_when_refresh_fails(tmp_path):
    """Regression test for the 2026-07-10 VM deploy incident: GGG's trade2
    API returns a persistent 403 to some cloud-hosted IPs. A refresh failure
    must fall back to whatever's on disk (even past its TTL) instead of
    crashing the whole meta job."""
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    cache_path = tmp_path / "stats_cache.json"

    http1 = _FakeHttpClient(payload)
    n1 = ModNormalizer(http_client=http1, cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n1.load()

    # Make the cache look expired, then simulate the endpoint being blocked.
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    envelope["fetched_at"] = 0
    cache_path.write_text(json.dumps(envelope), encoding="utf-8")

    class _FailingHttpClient:
        def get_json(self, url):
            raise RuntimeError("403 Forbidden")

    n2 = ModNormalizer(http_client=_FailingHttpClient(), cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n2.load()  # must not raise

    result = n2.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


def test_no_cache_and_refresh_fails_raises(tmp_path):
    class _FailingHttpClient:
        def get_json(self, url):
            raise RuntimeError("403 Forbidden")

    n = ModNormalizer(
        http_client=_FailingHttpClient(), cache_path=tmp_path / "nope.json", unmapped_log_path=tmp_path / "u.log"
    )
    with pytest.raises(RuntimeError, match="403"):
        n.load()


def test_ggg_direct_writes_disk_cache_for_later_fallback(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    cache_path = tmp_path / "stats_cache.json"

    n1 = ModNormalizer(http_client=http, cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n1.load()
    assert http.calls == 1
    assert n1.stats_source == "ggg_direct"
    assert cache_path.exists()

    result = n1.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


class _FirebaseFakeHttpClient:
    """Distinguishes the Firebase stats-cache URL from the GGG endpoint so
    tests can tell which tier actually served the data."""

    def __init__(self, firebase_envelope=None, ggg_payload=None, firebase_should_fail=False):
        self._firebase_envelope = firebase_envelope
        self._ggg_payload = ggg_payload
        self._firebase_should_fail = firebase_should_fail
        self.firebase_calls = 0
        self.ggg_calls = 0

    def get_json(self, url):
        if "firebase" in url or "storage.googleapis.com" in url:
            self.firebase_calls += 1
            if self._firebase_should_fail:
                raise RuntimeError("404 not found")
            return self._firebase_envelope
        self.ggg_calls += 1
        if self._ggg_payload is None:
            raise RuntimeError("403 Forbidden")
        return self._ggg_payload


def test_firebase_used_first_when_configured(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FirebaseFakeHttpClient(firebase_envelope={"fetched_at": 1720000000.0, "data": payload})
    n = ModNormalizer(
        http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n.load()
    assert http.firebase_calls == 1
    assert http.ggg_calls == 0
    assert n.stats_source == "firebase"
    assert n.stats_fetched_at == 1720000000.0
    result = n.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


def test_falls_through_to_ggg_when_firebase_unavailable(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FirebaseFakeHttpClient(firebase_should_fail=True, ggg_payload=payload)
    n = ModNormalizer(
        http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n.load()
    assert http.firebase_calls == 1
    assert http.ggg_calls == 1
    assert n.stats_source == "ggg_direct"


def test_firebase_bad_shape_falls_through_to_ggg(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FirebaseFakeHttpClient(firebase_envelope={"fetched_at": 1720000000.0, "data": {"nope": True}}, ggg_payload=payload)
    n = ModNormalizer(
        http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n.load()
    assert n.stats_source == "ggg_direct"


def test_no_firebase_configured_goes_straight_to_ggg(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    n = ModNormalizer(http_client=http, cache_path=tmp_path / "c.json", unmapped_log_path=tmp_path / "u.log")
    n.load()
    assert n.stats_source == "ggg_direct"
    assert http.calls == 1


def test_disk_cache_is_last_resort_when_firebase_and_ggg_both_fail(tmp_path):
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    cache_path = tmp_path / "c.json"

    # Seed the disk cache via a working GGG-direct load first.
    n1 = ModNormalizer(http_client=_FakeHttpClient(payload), cache_path=cache_path, unmapped_log_path=tmp_path / "u.log")
    n1.load()

    # Now both Firebase and GGG fail -- must fall back to the seeded disk cache.
    http2 = _FirebaseFakeHttpClient(firebase_should_fail=True, ggg_payload=None)
    n2 = ModNormalizer(
        http_client=http2, cache_path=cache_path, unmapped_log_path=tmp_path / "u.log",
        firebase_stats_url="https://storage.googleapis.com/poe-data-hub/internal/stats_cache.json",
    )
    n2.load()  # must not raise
    assert n2.stats_source == "disk_cache_stale"
    result = n2.normalize("+87 to maximum Life", "explicit")
    assert result[0].stat_id == "explicit.stat_3299347043"


def test_multiline_hybrid_mod_matches_as_single_entry(normalizer: ModNormalizer):
    # "Adds Irradiated to a Map \n# use remaining" is stored as ONE two-line
    # template under a single stat_id — normalize() must try the whole raw
    # block first (longest-template-first) rather than splitting into two
    # lines and failing to match either.
    result = normalizer.normalize("Adds Irradiated to a Map \n3 use remaining", "implicit")
    assert len(result) == 1
    assert result[0].stat_id == "implicit.stat_4041853756"
    assert result[0].values == [3.0]


def test_plural_slot_text_matches_singular_template(normalizer: ModNormalizer):
    # Template is "Has # Charm Slot" (singular) but real item text pluralizes.
    result = normalizer.normalize("Has 3 Charm Slots", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_1416292992"
    assert result[0].values == [3.0]


def test_reduced_word_matches_increased_template_with_negated_value(normalizer: ModNormalizer):
    # Template only has "#% increased Attribute Requirements" -- the game
    # displays a negative roll as "reduced" instead of "increased -35%".
    result = normalizer.normalize("35% reduced Attribute Requirements", "explicit")
    assert len(result) == 1
    assert result[0].stat_id == "explicit.stat_3639275092"
    assert result[0].values == [-35.0]


def test_processed_and_unmapped_counters(normalizer: ModNormalizer):
    normalizer.normalize("+87 to maximum Life", "explicit")
    normalizer.normalize("This is definitely not a real mod #", "explicit")
    normalizer.normalize("+23% to Cold Resistance", "explicit")
    assert normalizer.processed_count == 3
    assert normalizer.unmapped_count == 1


def test_stat_dictionary_embeds_all_known_kinds(normalizer: ModNormalizer):
    entries = normalizer.stat_dictionary()
    kinds = {e["mod_kind"] for e in entries}
    assert kinds == {"explicit", "implicit", "fractured"}
    assert any(e["stat_id"] == "explicit.stat_3299347043" for e in entries)
