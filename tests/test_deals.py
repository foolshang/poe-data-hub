"""Unit tests for src/fetchers/deals.py against a real captured sample
(data/samples/pathofexile_home_deals.html, captured live 2026-07-12)."""
from __future__ import annotations

from pathlib import Path

from src.fetchers.deals import fetch_deals

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


class _FakeHttpClient:
    def __init__(self, text: str):
        self._text = text

    def get_text(self, url, headers=None):
        return self._text


def test_fetch_deals_parses_real_sample():
    html = (SAMPLES / "pathofexile_home_deals.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(html)

    deals = fetch_deals(http)
    assert len(deals) == 3
    names = {d["item_name"] for d in deals}
    assert "Premium Quad Stash Tab" in names
    for d in deals:
        assert d["link"].startswith("https://www.pathofexile.com/")
        assert d["discount_percent"] is not None
        assert d["points_price"] is not None
        assert d["ends_in"]


def test_fetch_deals_no_specials_section_returns_empty():
    http = _FakeHttpClient("<html><body>nothing here</body></html>")
    assert fetch_deals(http) == []
