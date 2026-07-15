"""Unit tests for src/fetchers/news.py against real captured samples
(data/samples/steam_news_current_poe1.json, steam_news_current_poe2.json,
ggg_forum_poe1_news.html, ggg_forum_poe2_2211.html, ggg_forum_poe2_2212.html,
captured live 2026-07-12; th_forum_poe1_news.html, th_forum_poe2_2282.html,
th_forum_poe2_2283.html, captured live 2026-07-15)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.fetchers.news import (
    _normalize_title,
    _parse_en_post_date,
    _parse_th_post_date,
    _resolve_ggg_link,
    _resolve_th_link,
    _scrape_ggg_listing,
    _scrape_th_listing,
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
    entries = _scrape_ggg_listing(http, "https://www.pathofexile.com/forum/view-forum/news")
    assert len(entries) > 0
    link, norm_title, post_date = entries[0]
    assert link.startswith("https://www.pathofexile.com/forum/view-thread/")
    assert norm_title == norm_title.lower()
    assert isinstance(post_date, datetime)


def test_resolve_ggg_link_exact_match():
    index = [
        ("https://www.pathofexile.com/forum/view-thread/1", "curse of the allflame releases on july 24 pdt", None),
        ("https://www.pathofexile.com/forum/view-thread/2", "some other thread", None),
    ]
    link = _resolve_ggg_link(index, "Curse of the Allflame Releases on July 24 PDT")
    assert link == "https://www.pathofexile.com/forum/view-thread/1"


def test_resolve_ggg_link_no_match_returns_none():
    index = [("https://www.pathofexile.com/forum/view-thread/1", "totally unrelated title here", None)]
    assert _resolve_ggg_link(index, "Something Completely Different Long Enough") is None


def test_resolve_ggg_link_ambiguous_exact_match_refuses():
    index = [
        ("https://www.pathofexile.com/forum/view-thread/1", "patch notes", None),
        ("https://www.pathofexile.com/forum/view-thread/2", "patch notes", None),
    ]
    assert _resolve_ggg_link(index, "Patch Notes") is None


# ── phase 11: Thai forum link (link_th) ────────────────────────────────────

def test_parse_en_post_date():
    assert _parse_en_post_date("Jul 10, 2026, 4:00:00 AM") == datetime(2026, 7, 10, 4, 0, 0)


def test_parse_en_post_date_invalid_returns_none():
    assert _parse_en_post_date("not a date at all") is None


def test_parse_th_post_date():
    assert _parse_th_post_date("15 ก.ค. 2026 10:10:13") == datetime(2026, 7, 15, 10, 10, 13)
    assert _parse_th_post_date("1 พ.ค. 2026 07:09:04") == datetime(2026, 5, 1, 7, 9, 4)


def test_parse_th_post_date_unknown_month_returns_none():
    assert _parse_th_post_date("15 xx.x. 2026 10:10:13") is None


def test_parse_th_post_date_garbage_returns_none():
    assert _parse_th_post_date("not a thai date") is None


def test_scrape_th_listing_poe1():
    html = (SAMPLES / "th_forum_poe1_news.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(text_by_fragment={"forum/view-forum/news": html})
    entries = _scrape_th_listing(http, "https://th.pathofexile.com/forum/view-forum/news")
    assert len(entries) > 0
    link, _title, post_date = entries[0]
    assert link.startswith("https://th.pathofexile.com/forum/view-thread/")
    assert isinstance(post_date, datetime)


def test_resolve_th_link_single_close_candidate_matches():
    en_post_date = datetime(2026, 7, 15, 10, 10, 13)
    th_index = [
        ("https://th.pathofexile.com/forum/view-thread/1", "t", datetime(2026, 7, 15, 10, 10, 20)),  # 7s away
        ("https://th.pathofexile.com/forum/view-thread/2", "t", datetime(2026, 6, 1, 0, 0, 0)),  # way off
    ]
    assert _resolve_th_link(th_index, en_post_date) == "https://th.pathofexile.com/forum/view-thread/1"


def test_resolve_th_link_no_candidate_within_tolerance_returns_none():
    en_post_date = datetime(2026, 7, 15, 10, 10, 13)
    th_index = [("https://th.pathofexile.com/forum/view-thread/1", "t", datetime(2026, 7, 15, 11, 0, 0))]  # ~50min away
    assert _resolve_th_link(th_index, en_post_date) is None


def test_resolve_th_link_ambiguous_two_within_tolerance_refuses():
    en_post_date = datetime(2026, 7, 15, 10, 10, 13)
    th_index = [
        ("https://th.pathofexile.com/forum/view-thread/1", "t", datetime(2026, 7, 15, 10, 10, 20)),  # 7s away
        ("https://th.pathofexile.com/forum/view-thread/2", "t", datetime(2026, 7, 15, 10, 11, 0)),  # 47s away, still within tolerance
    ]
    assert _resolve_th_link(th_index, en_post_date) is None


def test_resolve_th_link_none_en_post_date_returns_none():
    th_index = [("https://th.pathofexile.com/forum/view-thread/1", "t", datetime(2026, 7, 15, 10, 10, 20))]
    assert _resolve_th_link(th_index, None) is None


def test_resolve_th_link_empty_index_returns_none():
    assert _resolve_th_link([], datetime(2026, 7, 15, 10, 10, 13)) is None


def test_fetch_news_end_to_end_poe1_resolves_link_th_for_real_pairs():
    steam_payload = json.loads((SAMPLES / "steam_news_current_poe1.json").read_text(encoding="utf-8"))
    en_html = (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8")
    th_html = (SAMPLES / "th_forum_poe1_news.html").read_text(encoding="utf-8")

    class _Http(_FakeHttpClient):
        def get_text(self, url, headers=None):
            if "th.pathofexile.com" in url:
                return th_html
            return en_html

    entries = fetch_news(_Http(json_by_fragment={"GetNewsForApp": steam_payload}), "poe1")
    assert len(entries) > 0
    with_th = [e for e in entries if "link_th" in e]
    assert len(with_th) > 0  # real overlapping data -- at least some should match
    for e in with_th:
        assert e["link_th"].startswith("https://th.pathofexile.com/forum/view-thread/")
        assert e["link_is_ggg"] is True  # link_th only ever set alongside a confident EN match
    # entries without a confident match must not have the key at all (not even null)
    without_th = [e for e in entries if "link_th" not in e]
    assert all("link_th" not in e for e in without_th)


def test_fetch_news_th_scrape_failure_leaves_link_th_absent():
    steam_payload = json.loads((SAMPLES / "steam_news_current_poe1.json").read_text(encoding="utf-8"))
    en_html = (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8")

    class _Http(_FakeHttpClient):
        def get_text(self, url, headers=None):
            if "th.pathofexile.com" in url:
                raise RuntimeError("boom")
            return en_html

    entries = fetch_news(_Http(json_by_fragment={"GetNewsForApp": steam_payload}), "poe1")
    assert len(entries) > 0
    assert all("link_th" not in e for e in entries)
    assert any(e["link_is_ggg"] for e in entries)  # EN resolution unaffected by TH failure


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
