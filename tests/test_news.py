"""Unit tests for src/fetchers/news.py against real captured samples
(data/samples/steam_news_current_poe1.json, steam_news_current_poe2.json,
ggg_forum_poe1_news.html, ggg_forum_poe2_2211.html, ggg_forum_poe2_2212.html,
all captured live 2026-07-12)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fetchers.news import (
    _normalize_title,
    _resolve_ggg_link,
    _scrape_ggg_listing,
    categorize,
    clean_content,
    fetch_news,
    fetch_steam_news,
    is_mtx_sale,
)

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


class _FakeHttpClient:
    def __init__(self, json_by_fragment=None, text_by_fragment=None):
        self._json = json_by_fragment or {}
        self._text = text_by_fragment or {}

    def get_json(self, url, headers=None):
        for fragment, payload in self._json.items():
            if fragment in url:
                return payload
        raise AssertionError(f"unexpected JSON URL in test: {url}")

    def get_text(self, url, headers=None):
        for fragment, text in self._text.items():
            if fragment in url:
                return text
        raise AssertionError(f"unexpected HTML URL in test: {url}")


def test_categorize_patch_vs_mtx_vs_news():
    assert categorize("0.5.4b Hotfix 2") == "patch"
    assert categorize("Patch Notes for 3.28.0") == "patch"
    assert categorize("Weekend Sale: 20% off Stash Tabs") == "mtx"
    assert categorize("Path of Exile 2 Build Showcase") == "news"


def test_is_mtx_sale():
    assert is_mtx_sale("Mystery Box Weekend") is True
    assert is_mtx_sale("Build Showcase") is False


def test_clean_content_strips_bbcode_and_html():
    raw = "[b]Hello[/b] <br>world\n\n  trailing  "
    assert clean_content(raw) == "Hello\nworld\ntrailing"


def test_clean_content_empty_input():
    assert clean_content("") == ""
    assert clean_content(None) == ""


def test_normalize_title_strips_punctuation_and_case():
    assert _normalize_title("Path of Exile: Curse of the Allflame!") == "path of exile curse of the allflame"


def test_fetch_steam_news_poe1_shape():
    payload = json.loads((SAMPLES / "steam_news_current_poe1.json").read_text(encoding="utf-8"))
    http = _FakeHttpClient(json_by_fragment={"GetNewsForApp": payload})

    entries = fetch_steam_news(http, "poe1")
    assert len(entries) > 0
    for e in entries:
        assert e["id"]
        assert e["title"]
        assert e["category"] in ("news", "patch", "mtx")
        assert e["link"].startswith("http")
        assert e["link_is_ggg"] is False
        assert e["published"].endswith("Z")


def test_fetch_steam_news_filters_non_official_feedname():
    payload = json.loads((SAMPLES / "steam_news_current_poe1.json").read_text(encoding="utf-8"))
    http = _FakeHttpClient(json_by_fragment={"GetNewsForApp": payload})
    entries = fetch_steam_news(http, "poe1")
    raw_items = payload["appnews"]["newsitems"]
    non_official = [i for i in raw_items if i.get("feedname") != "steam_community_announcements"]
    assert len(non_official) > 0  # sample actually contains e.g. "PC Gamer" press items
    assert len(entries) < len(raw_items)


def test_fetch_steam_news_unknown_game_raises():
    with pytest.raises(ValueError, match="unknown game"):
        fetch_steam_news(_FakeHttpClient(), "poe3")


def test_fetch_steam_news_invalid_shape_raises(tmp_path):
    http = _FakeHttpClient(json_by_fragment={"GetNewsForApp": {"not": "expected"}})
    with pytest.raises(ValueError, match="validation failed"):
        fetch_steam_news(http, "poe1", failed_responses_dir=tmp_path)
    assert list(tmp_path.iterdir())  # failed response dumped


def test_scrape_ggg_listing_poe1():
    html = (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(text_by_fragment={"forum/view-forum/news": html})
    pairs = _scrape_ggg_listing(http, "https://www.pathofexile.com/forum/view-forum/news")
    assert len(pairs) > 0
    link, norm_title = pairs[0]
    assert link.startswith("https://www.pathofexile.com/forum/view-thread/")
    assert norm_title == norm_title.lower()


def test_resolve_ggg_link_exact_match():
    index = [
        ("https://www.pathofexile.com/forum/view-thread/1", "curse of the allflame releases on july 24 pdt"),
        ("https://www.pathofexile.com/forum/view-thread/2", "some other thread"),
    ]
    link = _resolve_ggg_link(index, "Curse of the Allflame Releases on July 24 PDT")
    assert link == "https://www.pathofexile.com/forum/view-thread/1"


def test_resolve_ggg_link_no_match_returns_none():
    index = [("https://www.pathofexile.com/forum/view-thread/1", "totally unrelated title here")]
    assert _resolve_ggg_link(index, "Something Completely Different Long Enough") is None


def test_resolve_ggg_link_ambiguous_exact_match_refuses():
    index = [
        ("https://www.pathofexile.com/forum/view-thread/1", "patch notes"),
        ("https://www.pathofexile.com/forum/view-thread/2", "patch notes"),
    ]
    assert _resolve_ggg_link(index, "Patch Notes") is None


def test_fetch_news_end_to_end_resolves_ggg_links():
    steam_payload = json.loads((SAMPLES / "steam_news_current_poe1.json").read_text(encoding="utf-8"))
    ggg_html = (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(
        json_by_fragment={"GetNewsForApp": steam_payload},
        text_by_fragment={"forum/view-forum/news": ggg_html},
    )

    entries = fetch_news(http, "poe1")
    assert len(entries) > 0
    # link never rewritten to th. -- approved decision, display concern for the consumer
    for e in entries:
        assert not e["link"].startswith("https://th.pathofexile.com")
    assert any(e["link_is_ggg"] for e in entries)


def test_fetch_news_ggg_scrape_failure_keeps_steam_link():
    steam_payload = json.loads((SAMPLES / "steam_news_current_poe1.json").read_text(encoding="utf-8"))

    class _FailingGgg(_FakeHttpClient):
        def get_text(self, url, headers=None):
            raise RuntimeError("boom")

    http = _FailingGgg(json_by_fragment={"GetNewsForApp": steam_payload})
    entries = fetch_news(http, "poe1")
    assert len(entries) > 0
    assert all(e["link_is_ggg"] is False for e in entries)
    assert all(e["link"].startswith("https://steamstore") or "steam" in e["link"] or e["link"].startswith("http") for e in entries)
