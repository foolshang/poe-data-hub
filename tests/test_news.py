"""Unit tests for src/fetchers/news.py (GGG-forum-sourced, 2026-07-18
redesign) against real captured samples:
data/samples/ggg_forum_poe1_news.html, ggg_forum_poe2_2211.html,
ggg_forum_poe2_2212.html, th_forum_poe1_news.html, th_forum_poe2_2282.html,
th_forum_poe2_2283.html (captured live 2026-07-15); ggg_thread_poe2_3985152.html
(the real "0.5.4c Patch Notes" thread that never made it to Steam) and
ggg_thread_poe1_3985393.html (captured live 2026-07-18); ggg_thread_malformed.html
(synthetic, no post-table structure)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.fetchers.news import (
    FORCED_PATCH_LISTING_URLS,
    _extract_thread_id,
    _localize_to_utc,
    _parse_en_post_date,
    _parse_th_post_date,
    _resolve_th_link,
    _scrape_ggg_listing,
    _scrape_th_listing,
    categorize,
    clean_content,
    fetch_forum_news,
    fetch_thread_content,
    is_mtx_sale,
)

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


class _FakeHttpClient:
    def __init__(self, text_by_fragment=None):
        self._text = text_by_fragment or {}

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


# ── timezone localization ───────────────────────────────────────────────

def test_localize_to_utc_converts_using_scraped_tz_name():
    naive = datetime(2026, 7, 17, 1, 30, 0)  # forum's own Asia/Bangkok display time
    result = _localize_to_utc(naive, "Asia/Bangkok")
    assert result == datetime(2026, 7, 16, 18, 30, 0, tzinfo=timezone.utc)


def test_localize_to_utc_none_naive_dt_returns_none():
    assert _localize_to_utc(None, "Asia/Bangkok") is None


def test_localize_to_utc_missing_tz_name_falls_back_to_utc():
    naive = datetime(2026, 7, 17, 1, 30, 0)
    result = _localize_to_utc(naive, None)
    assert result == datetime(2026, 7, 17, 1, 30, 0, tzinfo=timezone.utc)


def test_localize_to_utc_invalid_tz_name_falls_back_to_utc():
    naive = datetime(2026, 7, 17, 1, 30, 0)
    result = _localize_to_utc(naive, "Not/A_Real_Zone")
    assert result == datetime(2026, 7, 17, 1, 30, 0, tzinfo=timezone.utc)


# ── thread id extraction ────────────────────────────────────────────────

def test_extract_thread_id():
    assert _extract_thread_id("https://www.pathofexile.com/forum/view-thread/3985152") == "3985152"


def test_extract_thread_id_no_match_returns_none():
    assert _extract_thread_id("https://www.pathofexile.com/forum/view-forum/2212") is None


# ── listing scrape ──────────────────────────────────────────────────────

def test_scrape_ggg_listing_poe1():
    html = (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(text_by_fragment={"forum/view-forum/news": html})
    entries, tz_name = _scrape_ggg_listing(http, "https://www.pathofexile.com/forum/view-forum/news")
    assert len(entries) > 0
    link, title, post_date = entries[0]
    assert link.startswith("https://www.pathofexile.com/forum/view-thread/")
    assert title  # original (unescaped, not normalized) title text
    assert isinstance(post_date, datetime)
    assert tz_name == "Asia/Bangkok"


def test_scrape_th_listing_poe1():
    html = (SAMPLES / "th_forum_poe1_news.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(text_by_fragment={"forum/view-forum/news": html})
    entries, _tz_name = _scrape_th_listing(http, "https://th.pathofexile.com/forum/view-forum/news")
    assert len(entries) > 0
    link, _title, post_date = entries[0]
    assert link.startswith("https://th.pathofexile.com/forum/view-thread/")
    assert isinstance(post_date, datetime)


# ── link_th resolution (unchanged logic) ────────────────────────────────

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


# ── thread content scraping ─────────────────────────────────────────────

def test_fetch_thread_content_regular_layout_real_patch_note():
    """The real "0.5.4c Patch Notes" thread (3985152) -- confirmed live
    2026-07-18 to be missing from Steam entirely, the whole reason this
    redesign exists."""
    html = (SAMPLES / "ggg_thread_poe2_3985152.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(text_by_fragment={"view-thread/3985152": html})
    content = fetch_thread_content(http, "https://www.pathofexile.com/forum/view-thread/3985152")
    assert "0.5.4c Patch Notes" in content
    assert "Supporter Packs" in content
    # replies (e.g. "Wow") must not be included, only the first post
    assert "Wow" not in content


def test_fetch_thread_content_newspost_layout():
    """GGG's official "news"-style post layout (<tr class="newsPost">,
    no content-container class) -- different HTML shape than a regular
    reply-style thread, must still extract correctly."""
    html = (SAMPLES / "ggg_thread_poe1_3985393.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(text_by_fragment={"view-thread/3985393": html})
    content = fetch_thread_content(http, "https://www.pathofexile.com/forum/view-thread/3985393")
    assert "ExileCon" in content


def test_fetch_thread_content_malformed_page_raises():
    html = (SAMPLES / "ggg_thread_malformed.html").read_text(encoding="utf-8")
    http = _FakeHttpClient(text_by_fragment={"view-thread/1": html})
    with pytest.raises(ValueError, match="could not locate first-post content"):
        fetch_thread_content(http, "https://www.pathofexile.com/forum/view-thread/1")


# ── fetch_forum_news end-to-end ─────────────────────────────────────────

class _CountingHttpClient(_FakeHttpClient):
    """Tracks which thread-detail URLs were actually fetched, so tests can
    assert that already-known threads never trigger a content fetch."""

    def __init__(self, text_by_fragment=None):
        super().__init__(text_by_fragment)
        self.thread_fetches: list[str] = []

    def get_text(self, url, headers=None):
        if "/view-thread/" in url and "/view-forum/" not in url:
            self.thread_fetches.append(url)
        return super().get_text(url, headers=headers)


def _poe1_listing_http(extra_text_by_fragment=None):
    en_html = (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8")
    th_html = (SAMPLES / "th_forum_poe1_news.html").read_text(encoding="utf-8")
    text_by_fragment = {
        "th.pathofexile.com/forum/view-forum/news": th_html,
        "www.pathofexile.com/forum/view-forum/news": en_html,
    }
    if extra_text_by_fragment:
        text_by_fragment.update(extra_text_by_fragment)
    return text_by_fragment


def test_fetch_forum_news_new_thread_fetches_content():
    thread_html = (SAMPLES / "ggg_thread_poe2_3985152.html").read_text(encoding="utf-8")
    http = _CountingHttpClient(_poe1_listing_http({"view-thread/3982460": thread_html}))

    entries = fetch_forum_news(http, "poe1", previous_entries=[])
    assert len(entries) > 0
    assert any(http.thread_fetches)  # at least one new-thread content fetch happened
    new_entry = next(e for e in entries if e["id"] == "forum-3982460")
    assert new_entry["content"]  # got real content from the thread page
    assert new_entry["link"] == "https://www.pathofexile.com/forum/view-thread/3982460"
    assert new_entry["id"] not in ("", None)


def test_fetch_forum_news_known_thread_skips_content_fetch():
    http = _CountingHttpClient(_poe1_listing_http())
    previous_entries = [{"id": "forum-3982460", "content": "previously scraped content"}]

    entries = fetch_forum_news(http, "poe1", previous_entries=previous_entries)
    known_entry = next(e for e in entries if e["id"] == "forum-3982460")
    assert known_entry["content"] == "previously scraped content"
    assert not any("3982460" in u for u in http.thread_fetches)


def test_fetch_forum_news_content_scrape_failure_still_emits_entry():
    http = _CountingHttpClient(_poe1_listing_http({"view-thread/3982460": "<html>no post table here</html>"}))

    entries = fetch_forum_news(http, "poe1", previous_entries=[])
    new_entry = next(e for e in entries if e["id"] == "forum-3982460")
    assert new_entry["content"] == ""
    assert new_entry["title"]
    assert new_entry["link"]
    assert new_entry["category"] in ("news", "patch", "mtx")


def test_fetch_forum_news_empty_listing_raises():
    http = _FakeHttpClient(text_by_fragment={"forum/view-forum/news": "<html>nothing here</html>"})
    with pytest.raises(ValueError, match="no threads"):
        fetch_forum_news(http, "poe1", previous_entries=[])


def test_fetch_forum_news_listing_scrape_exception_raises():
    class _FailingHttp(_FakeHttpClient):
        def get_text(self, url, headers=None):
            raise RuntimeError("boom")

    with pytest.raises(ValueError, match="no threads"):
        fetch_forum_news(_FailingHttp(), "poe1", previous_entries=[])


def test_fetch_forum_news_unknown_game_raises():
    with pytest.raises(ValueError, match="unknown game"):
        fetch_forum_news(_FakeHttpClient(), "poe3", previous_entries=[])


def test_fetch_forum_news_resolves_link_th_for_real_pairs():
    http = _CountingHttpClient(_poe1_listing_http())
    previous_entries = [{"id": "forum-3982460", "content": "c"}, {"id": "forum-3982050", "content": "c"}]

    entries = fetch_forum_news(http, "poe1", previous_entries=previous_entries)
    assert len(entries) > 0
    with_th = [e for e in entries if "link_th" in e]
    assert len(with_th) > 0  # real overlapping data -- at least some should match
    for e in with_th:
        assert e["link_th"].startswith("https://th.pathofexile.com/forum/view-thread/")
    without_th = [e for e in entries if "link_th" not in e]
    assert all("link_th" not in e for e in without_th)  # never a null placeholder


def test_fetch_forum_news_th_scrape_failure_leaves_link_th_absent():
    en_html = (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8")

    class _Http(_CountingHttpClient):
        def get_text(self, url, headers=None):
            if "th.pathofexile.com" in url:
                raise RuntimeError("boom")
            return super().get_text(url, headers=headers)

    listing_entries, _tz = _scrape_ggg_listing(
        _FakeHttpClient({"forum/view-forum/news": en_html}), "https://www.pathofexile.com/forum/view-forum/news"
    )
    # mark every listed thread already-known so this test only exercises
    # link_th failure handling, not content fetching.
    previous_entries = [{"id": f"forum-{_extract_thread_id(link)}", "content": "c"} for link, _t, _d in listing_entries]
    http = _Http({"www.pathofexile.com/forum/view-forum/news": en_html})
    entries = fetch_forum_news(http, "poe1", previous_entries=previous_entries)
    assert len(entries) > 0
    assert all("link_th" not in e for e in entries)


# ── 2026-07-19: PoE1 patch-notes/poe-press/dev-manifesto categories ────────
# (audit found PoE1 3.29.0 patch notes missing entirely -- GGG posts PoE1
# patch notes to a dedicated "patch-notes" forum, separate from "news",
# unlike the old Steam feed which merged everything into one stream.)

def _poe1_full_listing_http(extra_text_by_fragment=None):
    text_by_fragment = {
        "th.pathofexile.com/forum/view-forum/news": (SAMPLES / "th_forum_poe1_news.html").read_text(encoding="utf-8"),
        "www.pathofexile.com/forum/view-forum/news": (SAMPLES / "ggg_forum_poe1_news.html").read_text(encoding="utf-8"),
        "www.pathofexile.com/forum/view-forum/patch-notes": (SAMPLES / "ggg_forum_poe1_patch-notes.html").read_text(encoding="utf-8"),
        "th.pathofexile.com/forum/view-forum/patch-notes": (SAMPLES / "th_forum_poe1_patch-notes.html").read_text(encoding="utf-8"),
        "www.pathofexile.com/forum/view-forum/poe-press": (SAMPLES / "ggg_forum_poe1_poe-press.html").read_text(encoding="utf-8"),
        "www.pathofexile.com/forum/view-forum/dev-manifesto": (SAMPLES / "ggg_forum_poe1_dev-manifesto.html").read_text(encoding="utf-8"),
        "th.pathofexile.com/forum/view-forum/1010": (SAMPLES / "th_forum_poe1_dev-manifesto.html").read_text(encoding="utf-8"),
    }
    if extra_text_by_fragment:
        text_by_fragment.update(extra_text_by_fragment)
    return text_by_fragment


_POE1_EN_SAMPLE_FILES = (
    "ggg_forum_poe1_news.html",
    "ggg_forum_poe1_patch-notes.html",
    "ggg_forum_poe1_poe-press.html",
    "ggg_forum_poe1_dev-manifesto.html",
)


def _poe1_all_threads_known() -> list[dict]:
    """previous_entries marking every thread across all 4 EN listing
    samples as already-known, so a fetch_forum_news test can exercise
    listing/category/link_th logic without a thread-detail-page mock for
    every single thread."""
    previous_entries = []
    for fname in _POE1_EN_SAMPLE_FILES:
        html_text = (SAMPLES / fname).read_text(encoding="utf-8")
        entries, _tz = _scrape_ggg_listing(_FakeHttpClient({"x": html_text}), "https://www.pathofexile.com/forum/view-forum/x")
        previous_entries += [{"id": f"forum-{_extract_thread_id(link)}", "content": "c"} for link, _t, _d in entries]
    return previous_entries


def test_fetch_forum_news_poe1_includes_all_four_categories():
    http = _CountingHttpClient(_poe1_full_listing_http())
    entries = fetch_forum_news(http, "poe1", previous_entries=_poe1_all_threads_known())
    ids = {e["id"] for e in entries}
    assert "forum-3985332" in ids  # 3.29.0 patch notes, patch-notes forum
    assert "forum-3985355" in ids  # poe-press announcement
    assert "forum-3292308" in ids  # dev-manifesto (old but real)
    assert not any(http.thread_fetches)  # every thread pre-marked known


def test_fetch_forum_news_poe1_forces_patch_category_for_patch_notes_forum():
    http = _CountingHttpClient(_poe1_full_listing_http())
    entries = fetch_forum_news(http, "poe1", previous_entries=_poe1_all_threads_known())
    by_id = {e["id"]: e for e in entries}
    assert by_id["forum-3985332"]["category"] == "patch"  # forced, regardless of title
    # poe-press/dev-manifesto are NOT forced -- default title heuristic applies
    assert by_id["forum-3985355"]["category"] == "news"
    assert by_id["forum-3292308"]["category"] == "news"


def test_fetch_forum_news_poe1_link_th_resolves_for_patch_notes_and_dev_manifesto():
    http = _CountingHttpClient(_poe1_full_listing_http())
    entries = fetch_forum_news(http, "poe1", previous_entries=_poe1_all_threads_known())
    by_id = {e["id"]: e for e in entries}
    # real confirmed pair: EN 3985332 (3.29.0 patch notes) <-> TH 3985358, 0s apart
    assert by_id["forum-3985332"]["link_th"] == "https://th.pathofexile.com/forum/view-thread/3985358"
    # real confirmed pair: EN 3292308 (dev-manifesto) <-> TH 3292313, 24s apart
    assert by_id["forum-3292308"]["link_th"] == "https://th.pathofexile.com/forum/view-thread/3292313"
    # poe-press has no configured Thai counterpart category at all
    assert "link_th" not in by_id["forum-3985355"]


def test_fetch_forum_news_poe2_forces_patch_category_for_2212():
    listing_2211 = (SAMPLES / "ggg_forum_poe2_2211.html").read_text(encoding="utf-8")
    listing_2212 = (SAMPLES / "ggg_forum_poe2_2212.html").read_text(encoding="utf-8")
    th_2282 = (SAMPLES / "th_forum_poe2_2282.html").read_text(encoding="utf-8")
    th_2283 = (SAMPLES / "th_forum_poe2_2283.html").read_text(encoding="utf-8")
    http = _CountingHttpClient({
        "www.pathofexile.com/forum/view-forum/2211": listing_2211,
        "www.pathofexile.com/forum/view-forum/2212": listing_2212,
        "th.pathofexile.com/forum/view-forum/2282": th_2282,
        "th.pathofexile.com/forum/view-forum/2283": th_2283,
    })
    # avoid content-fetch dependence on live thread pages: pretend every
    # thread is already known so this test only exercises listing+category.
    _, tz = _scrape_ggg_listing(http, "https://www.pathofexile.com/forum/view-forum/2212")
    entries_2212, _ = _scrape_ggg_listing(http, "https://www.pathofexile.com/forum/view-forum/2212")
    previous_entries = [
        {"id": f"forum-{_extract_thread_id(link)}", "content": "c"} for link, _t, _d in entries_2212
    ]
    entries_2211, _ = _scrape_ggg_listing(http, "https://www.pathofexile.com/forum/view-forum/2211")
    previous_entries += [
        {"id": f"forum-{_extract_thread_id(link)}", "content": "c"} for link, _t, _d in entries_2211
    ]

    entries = fetch_forum_news(http, "poe2", previous_entries=previous_entries)
    assert len(entries) > 0
    by_id = {e["id"]: e for e in entries}
    for link, _title, _date in entries_2212:
        thread_id = _extract_thread_id(link)
        assert by_id[f"forum-{thread_id}"]["category"] == "patch"


def test_forced_patch_listing_urls_includes_both_games_patch_notes_forums():
    assert FORCED_PATCH_LISTING_URLS == {
        "https://www.pathofexile.com/forum/view-forum/2212",
        "https://www.pathofexile.com/forum/view-forum/patch-notes",
    }
