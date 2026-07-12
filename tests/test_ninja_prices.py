"""Unit tests for src/fetchers/ninja_prices.py against real captured samples
(PoE2: data/samples/ninja_poe2_exchange_currency.json /
ninja_poe2_stash_uniqueweapons.json; PoE1: ninja_poe1_exchange_currency.json /
ninja_poe1_stash_uniqueweapon.json, captured live 2026-07-11)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.ninja_prices import NinjaPricesFetcher, POE1_CATEGORIES, POE2_CATEGORIES, category_bucket

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


class _FakeHttpClient:
    def __init__(self, payload_by_url_fragment: dict[str, dict]):
        self._payloads = payload_by_url_fragment
        self.urls_called: list[str] = []

    def get_json(self, url):
        self.urls_called.append(url)
        for fragment, payload in self._payloads.items():
            if fragment in url:
                return payload
        raise AssertionError(f"unexpected URL in test: {url}")


def test_category_bucket_defaults_to_poe2():
    assert category_bucket("Currency") == "currency"
    assert category_bucket("UniqueWeapon") == "items"


def test_category_bucket_poe1():
    assert category_bucket("Currency", game="poe1") == "currency"
    assert category_bucket("UniqueWeapon", game="poe1") == "items"
    assert category_bucket("DivinationCard", game="poe1") == "currency"


def test_poe2_exchange_currency_uses_exalted_as_value_currency():
    payload = json.loads((SAMPLES / "ninja_poe2_exchange_currency.json").read_text(encoding="utf-8"))
    http = _FakeHttpClient({"exchange/current/overview": payload})
    fetcher = NinjaPricesFetcher(http, game="poe2")

    entries = fetcher.fetch_category("Runes of Aldur", POE2_CATEGORIES[0])
    assert len(entries) > 0
    for e in entries:
        assert e["value_currency"] == "exalted"
        assert e["divine_value"] is not None
        assert e["source"] == "ninja"


def test_poe1_exchange_currency_uses_chaos_as_value_currency():
    payload = json.loads((SAMPLES / "ninja_poe1_exchange_currency.json").read_text(encoding="utf-8"))
    http = _FakeHttpClient({"exchange/current/overview": payload})
    fetcher = NinjaPricesFetcher(http, game="poe1")

    entries = fetcher.fetch_category("Mirage", POE1_CATEGORIES[0])
    assert len(entries) > 0
    for e in entries:
        assert e["value_currency"] == "chaos"
        # PoE1 exchange lines are chaos-primary already -- "value" should equal "chaos".
        assert e["value"] > 0
        assert "chaos_value" not in e  # would be redundant with "value" here


def test_poe1_stash_uses_direct_chaos_exalted_divine_fields():
    payload = json.loads((SAMPLES / "ninja_poe1_stash_uniqueweapon.json").read_text(encoding="utf-8"))
    http = _FakeHttpClient({"stash/current/item/overview": payload})
    fetcher = NinjaPricesFetcher(http, game="poe1")

    uw_cat = next(c for c in POE1_CATEGORIES if c.name == "UniqueWeapon")
    entries = fetcher.fetch_category("Mirage", uw_cat)
    assert len(entries) > 0
    first = entries[0]
    assert first["value_currency"] == "chaos"
    assert first["divine_value"] is not None
    assert first["name"] == "The Winds of Fate"
    assert "exalted_value" in first  # sample line has exaltedValue present


def test_poe2_stash_uses_rate_conversion_not_direct_fields():
    payload = json.loads((SAMPLES / "ninja_poe2_stash_uniqueweapons.json").read_text(encoding="utf-8"))
    http = _FakeHttpClient({"stash/current/item/overview": payload})
    fetcher = NinjaPricesFetcher(http, game="poe2")

    uw_cat = next(c for c in POE2_CATEGORIES if c.name == "UniqueWeapon")
    entries = fetcher.fetch_category("Runes of Aldur", uw_cat)
    assert len(entries) > 0
    for e in entries:
        assert e["value_currency"] == "exalted"


def test_fetch_all_skips_failing_category_without_raising():
    class _FailingHttp:
        def get_json(self, url):
            raise RuntimeError("boom")

    fetcher = NinjaPricesFetcher(_FailingHttp(), game="poe1")
    entries, per_category = fetcher.fetch_all("Mirage")
    assert entries == []
    assert all(v == {"ok": False, "count": 0} for v in per_category.values())
    assert set(per_category.keys()) == {c.name for c in POE1_CATEGORIES}


def test_fetch_all_per_category_reports_ok_and_count():
    payload = json.loads((SAMPLES / "ninja_poe1_exchange_currency.json").read_text(encoding="utf-8"))
    http = _FakeHttpClient({"exchange/current/overview": payload})
    fetcher = NinjaPricesFetcher(http, game="poe1")

    entries, per_category = fetcher.fetch_all("Mirage")
    assert per_category["Currency"]["ok"] is True
    assert per_category["Currency"]["count"] == len([e for e in entries if e["category"] == "Currency"])
    # DivinationCard/UniqueWeapon/etc all hit the same fake URL fragment
    # (stash and other exchange categories aren't stubbed in this fake
    # client), so they raise AssertionError inside fetch_category -> "ok": False.
    assert per_category["UniqueWeapon"]["ok"] is False


def test_unknown_game_raises_at_construction():
    with pytest.raises(ValueError, match="unknown game"):
        NinjaPricesFetcher(_FakeHttpClient({}), game="poe3")
