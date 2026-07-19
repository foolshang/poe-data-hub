"""Fetch PoE1/PoE2 news from GGG's own forum (2026-07-18 redesign --
replaces Steam News entirely as the source of news entries; see git history
for the phase-8/phase-11 Steam-sourced version this replaced).

Steam News was dropped because it isn't a reliable primary source: a real
patch note appeared on the forum with no Steam post at all (GGG thread
3985152, "0.5.4c Patch Notes", posted 2026-07-17 -- Steam's feed never got
it, confirmed live 2026-07-18). The forum listing pages (`GGG_LISTING_URLS`)
were already being scraped here to *upgrade* Steam links to real GGG
threads (phase 8); that listing scrape is now the sole source, and each
thread's own page is scraped for its first-post content -- but only for
threads not already known from the previous publish (`previous_entries` in
`fetch_forum_news`), since re-scraping every thread on the listing every
5-minute cycle would be wasteful and threads that already have content
don't need it again.

id scheme change (breaking): entries are now `"forum-{thread_id}"` instead
of a bare Steam `gid`. Downstream consumers (e.g. the Discord bot's
`seen.json`) must migrate before this ships to production -- see SPEC.md
section 8.4's "required deploy order" callout. This module does not gate
that rollout; it's a hub-side change only.

Timezone gotcha (would have silently corrupted `published` if missed):
every GGG forum page (listing or thread, `www.` or `th.`) embeds
`window.momentTimezone = '<IANA name>';` in a <script> block, and the
displayed `post_date` text is rendered in *that* timezone -- confirmed live
2026-07-18 to be `Asia/Bangkok` from this vantage point (almost certainly
GeoIP-dependent, so `_localize_to_utc` reads the tz name fresh from each
scraped page rather than hardcoding it -- a deploy VM in a different region
may see a different value). Confirmed two ways: Steam's real UTC date for
"0.5.4b Patch Notes" was ~6.5h behind that same thread's own forum
post_date; the HTTP response `Date` header (true UTC) vs. the most recent
reply's displayed post_date on an active thread showed a consistent ~5h+
gap, consistent with a UTC+7 display clock. `_parse_en_post_date`/
`_parse_th_post_date` still return naive datetimes on purpose -- they're
also used for `link_th`'s EN<->TH time-proximity matching, which only ever
needs relative comparison within the same forum display clock, never an
absolute value.

Thai forum link (`link_th`, phase 11, matching logic unchanged by this
redesign -- see SPEC.md for the full narrative): th.pathofexile.com carries
genuinely separate, independently-translated threads for some but not all
news. Matching is by post_date proximity between the English thread's own
post_date and the Thai listing's post_date (`TH_MATCH_TOLERANCE_SECONDS`),
attempted for every entry now (no more "only if a confident title-match
already upgraded the Steam link" gate -- every entry already *is* a GGG
thread by construction).
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

GGG_LISTING_URLS: dict[str, list[str]] = {
    # 2026-07-19: audited every category on both games' live forum index
    # (not just news-adjacent ones) after PoE1's 3.29.0 patch notes were
    # found missing -- GGG posts PoE1 patch notes to their own dedicated
    # "patch-notes" forum, separate from "news", unlike the old Steam feed
    # which merged everything into one stream regardless of forum category.
    # poe-press and dev-manifesto are both genuine first-party GGG content
    # (not third-party press despite the "press" name -- confirmed by
    # reading actual thread content) that were never covered either.
    # Deliberately NOT added: ps4-patch-notes/xbox-patch-notes (console-
    # specific mirrors of the same PC patch notes, redundant for this feed)
    # and every trade/PvP/guild/bug-report/discussion forum (not news).
    "poe1": [
        "https://www.pathofexile.com/forum/view-forum/news",
        "https://www.pathofexile.com/forum/view-forum/patch-notes",
        "https://www.pathofexile.com/forum/view-forum/poe-press",
        "https://www.pathofexile.com/forum/view-forum/dev-manifesto",
    ],
    "poe2": [
        "https://www.pathofexile.com/forum/view-forum/2211",  # announcements
        "https://www.pathofexile.com/forum/view-forum/2212",  # patch notes
    ],
}
GGG_WWW_DOMAIN = "https://www.pathofexile.com"
# PoE2's 2212 and PoE1's "patch-notes" are GGG's dedicated Patch Notes
# forums -- threads scraped from either are unambiguously "patch" regardless
# of title wording (e.g. a bare "Hotfix 2" title wouldn't otherwise match
# categorize()'s title heuristic). poe-press/dev-manifesto are NOT forced --
# their titles already read naturally as "news" via the default heuristic.
FORCED_PATCH_LISTING_URLS = {
    "https://www.pathofexile.com/forum/view-forum/2212",
    "https://www.pathofexile.com/forum/view-forum/patch-notes",
}

# Captures (href, title, "<postBy post_date span content>") in one pass --
# the post_date is the thread's *own* creation time (from its "postBy" line,
# not "last_post" which can be a later reply).
GGG_TITLE_LINK_DATE_RE = re.compile(
    r'<div class="title">\s*<a href="([^"]+)">\s*([^<]+?)\s*</a>.*?<span class="post_date">,\s*([^<]+)</span>',
    re.S,
)
_THREAD_ID_RE = re.compile(r"/forum/view-thread/(\d+)")
_MOMENT_TZ_RE = re.compile(r"window\.momentTimezone\s*=\s*'([^']+)'")

# pathofexile.com returns 403 for a bare "Mozilla/5.0" -- needs a full
# browser-looking UA.
GGG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Thai forum categories are NOT the same forum ids as English -- th. on
# English PoE2's 2211/2212 returns English content verbatim (just Thai UI
# chrome), not translations. Real Thai PoE2 categories: 2282 = "Early
# Access Announcements", 2283 = "Patch Details". PoE1's "news" and
# "patch-notes" categories *are* the same URL path/slug on th. (genuinely
# translated there, confirmed live 2026-07-19 -- e.g. thread 3985358 is the
# real Thai 3.29.0 patch notes, found via th.pathofexile.com's own patch-
# notes listing). PoE1's dev-manifesto has a real Thai counterpart too, but
# NOT at the same slug -- it's forum id 1010 ("แถลงการณ์จากทีมพัฒนา",
# confirmed by comparing thread titles/dates against the English versions).
# poe-press has no Thai counterpart at all (checked the full th. forum nav
# menu) -- link_th simply never resolves for those entries, same graceful
# "no match = no field" behavior as any other unmatched entry, nothing
# special needed here.
TH_GGG_LISTING_URLS: dict[str, list[str]] = {
    "poe1": [
        "https://th.pathofexile.com/forum/view-forum/news",
        "https://th.pathofexile.com/forum/view-forum/patch-notes",
        "https://th.pathofexile.com/forum/view-forum/1010",  # dev-manifesto
    ],
    "poe2": [
        "https://th.pathofexile.com/forum/view-forum/2282",
        "https://th.pathofexile.com/forum/view-forum/2283",
    ],
}
TH_DOMAIN = "https://th.pathofexile.com"

# RTGS-standard Thai month abbreviations as used on th.pathofexile.com.
_TH_MONTH_ABBR: dict[str, int] = {
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}
# e.g. "15 ก.ค. 2026 10:10:13" (day, Thai month abbr, year, 24h time -- no
# comma separators, unlike the English "Jul 10, 2026, 4:00:00 AM" style).
_TH_POST_DATE_RE = re.compile(r"(\d{1,2})\s+(\S+)\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})")

# Empirically strict on purpose (project owner: "จับคู่ไม่มั่นใจ = ไม่ใส่ field
# เด็ดขาด" -- uncertain match = never include the field). Every real EN<->TH
# pair found live (both games) was within 60 seconds of each other -- 300s
# leaves generous headroom above every observed real gap while staying far
# tighter than "same day". See SPEC.md for the actual measured examples.
TH_MATCH_TOLERANCE_SECONDS = 300

MTX_KEYWORDS = ["sale", "deal", "special", "discount", "% off", "mystery box", "weekend", "stash tab"]
BBCODE_TAG_RE = re.compile(r"\[/?[^\]]+\]")
VERSION_PREFIX_RE = re.compile(r"^\d+\.\d+")


def clean_content(raw: str) -> str:
    if not raw:
        return ""
    text = BBCODE_TAG_RE.sub("", raw)
    text = BeautifulSoup(text, "html.parser").get_text(separator="\n")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def is_mtx_sale(title: str) -> bool:
    t = title.lower()
    return any(keyword in t for keyword in MTX_KEYWORDS)


def categorize(title: str) -> str:
    if "patch" in title.lower() or VERSION_PREFIX_RE.match(title.strip()):
        return "patch"
    if is_mtx_sale(title):
        return "mtx"
    return "news"


def _parse_en_post_date(text: str) -> Optional[datetime]:
    """Parses the English forum's "Jul 10, 2026, 4:00:00 AM" style post_date
    text. Naive datetime on purpose (no tzinfo) -- it's the forum's own
    display-local time, not UTC (see module docstring); `_localize_to_utc`
    is what converts it to an actual UTC value using the tz name scraped
    from the same page."""
    try:
        return dateutil_parser.parse(text.strip())
    except (ValueError, OverflowError, TypeError):
        return None


def _parse_th_post_date(text: str) -> Optional[datetime]:
    """Parses the Thai forum's "15 ก.ค. 2026 10:10:13" style post_date text."""
    m = _TH_POST_DATE_RE.search(text.strip())
    if not m:
        return None
    day, month_abbr, year, hour, minute, second = m.groups()
    month = _TH_MONTH_ABBR.get(month_abbr)
    if month is None:
        return None
    try:
        return datetime(int(year), month, int(day), int(hour), int(minute), int(second))
    except ValueError:
        return None


def _localize_to_utc(naive_dt: Optional[datetime], tz_name: Optional[str]) -> Optional[datetime]:
    """Attaches the timezone the forum actually displayed `naive_dt` in
    (read fresh from the same page via `_MOMENT_TZ_RE`, see module
    docstring) and converts to UTC. Falls back to treating `naive_dt` as if
    it were already UTC -- logged, never silent -- only when `tz_name` is
    missing or unrecognized, same "never drop the entry, degrade precision
    instead" policy used for a content-scrape failure."""
    if naive_dt is None:
        return None
    if tz_name:
        try:
            return naive_dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
        except Exception:
            log.warning("news: unknown/invalid forum timezone %r, treating post_date as UTC", tz_name)
    else:
        log.warning("news: no momentTimezone found on scraped page, treating post_date as UTC")
    return naive_dt.replace(tzinfo=timezone.utc)


def _extract_thread_id(link: str) -> Optional[str]:
    m = _THREAD_ID_RE.search(link)
    return m.group(1) if m else None


def _scrape_listing_with_dates(
    http: HttpClient, url: str, domain: str, parse_date_fn,
) -> tuple[list[tuple[str, str, Optional[datetime]]], Optional[str]]:
    text = http.get_text(url, headers=GGG_HEADERS)
    tz_match = _MOMENT_TZ_RE.search(text)
    tz_name = tz_match.group(1) if tz_match else None
    entries = []
    for href, title, date_text in GGG_TITLE_LINK_DATE_RE.findall(text):
        link = href if href.startswith("http") else f"{domain}{href}"
        entries.append((link, html.unescape(title).strip(), parse_date_fn(date_text)))
    return entries, tz_name


def _scrape_ggg_listing(http: HttpClient, url: str) -> tuple[list[tuple[str, str, Optional[datetime]]], Optional[str]]:
    return _scrape_listing_with_dates(http, url, GGG_WWW_DOMAIN, _parse_en_post_date)


def _scrape_th_listing(http: HttpClient, url: str) -> tuple[list[tuple[str, str, Optional[datetime]]], Optional[str]]:
    return _scrape_listing_with_dates(http, url, TH_DOMAIN, _parse_th_post_date)


def _build_ggg_listing(http: HttpClient, game: str) -> list[dict[str, Any]]:
    """Scrapes every EN listing URL configured for `game` into a flat list
    of raw listing items (link/title/post_date/tz_name/source_url). A
    single URL's scrape failure is logged and skipped, never aborts the
    whole game's listing -- `fetch_forum_news` is what decides whether the
    *combined* result is unusable."""
    raw: list[dict[str, Any]] = []
    for url in GGG_LISTING_URLS.get(game, []):
        try:
            entries, tz_name = _scrape_ggg_listing(http, url)
        except Exception:
            log.warning("news(%s): failed to scrape %s", game, url, exc_info=True)
            continue
        for link, title, post_date in entries:
            raw.append({"link": link, "title": title, "post_date": post_date, "tz_name": tz_name, "source_url": url})
    return raw


def _build_th_link_index(http: HttpClient, game: str) -> list[tuple[str, str, Optional[datetime]]]:
    index: list[tuple[str, str, Optional[datetime]]] = []
    for url in TH_GGG_LISTING_URLS.get(game, []):
        try:
            entries, _tz_name = _scrape_th_listing(http, url)
            index.extend(entries)
        except Exception:
            log.warning("news(%s): failed to scrape %s for TH links", game, url, exc_info=True)
    return index


def _resolve_th_link(th_index: list[tuple[str, str, Optional[datetime]]], en_post_date: Optional[datetime]) -> Optional[str]:
    """Strict time-proximity match: a Thai thread is a confident match only
    if it's the *single* Thai candidate within TH_MATCH_TOLERANCE_SECONDS of
    the English thread's own post_date. Two or more candidates within
    tolerance is ambiguous -> None, never a guess."""
    if en_post_date is None or not th_index:
        return None

    candidates = [
        (link, abs((post_date - en_post_date).total_seconds()))
        for link, _title, post_date in th_index
        if post_date is not None
    ]
    within_tolerance = [(link, delta) for link, delta in candidates if delta <= TH_MATCH_TOLERANCE_SECONDS]

    if len(within_tolerance) == 1:
        return within_tolerance[0][0]
    if len(within_tolerance) > 1:
        log.info(
            "news: %d TH candidates within %ds of en_post_date=%s, ambiguous -- no link_th",
            len(within_tolerance), TH_MATCH_TOLERANCE_SECONDS, en_post_date,
        )
    return None


def fetch_thread_content(http: HttpClient, thread_url: str) -> str:
    """Scrapes the first post's body text from a GGG thread page. Layout
    varies: the "regular" reply layout puts the post body in
    `td.content-container div.content`, but GGG's own official "news"-style
    posts (image+text boxes, confirmed live on thread 3985393) use a
    different first `<tr>` shape (`<tr class="newsPost"><td colspan="2">`)
    with no `content-container` class at all. Scoping to "the first `<tr>`
    inside the post-list table, then whatever `.content` div is inside it"
    handles both without hardcoding either specific layout. Raises if
    neither shape is found (page structure changed further, or a
    non-thread page) -- the caller decides how to degrade."""
    text = http.get_text(thread_url, headers=GGG_HEADERS)
    soup = BeautifulSoup(text, "html.parser")
    first_row = soup.select_one("table.forumTable.forumPostListTable tr")
    content_div = first_row.select_one(".content") if first_row else None
    if content_div is None:
        raise ValueError(f"news: could not locate first-post content on {thread_url}")
    return clean_content(content_div.decode_contents())


def fetch_forum_news(
    http: HttpClient, game: str, previous_entries: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """The GGG forum listing(s) for `game`, one entry per thread, sorted
    newest-first. `id` is `"forum-{thread_id}"`. `content` is scraped from
    each thread's own page -- but only for threads whose `id` isn't already
    present in `previous_entries` (the previous publish's entries, passed
    in by the caller from `publisher.fetch_previous`); already-known
    threads keep their previously-scraped `content` unchanged, so a normal
    5-minute cycle only pays for genuinely new threads (0-2, typically).

    A content-scrape failure for a *new* thread never drops the entry --
    title/link/category/published come from the (separate) listing scrape,
    so the entry is still emitted with `content=""` and a logged warning.

    Raises if every configured listing URL fails or the combined scrape is
    empty (GGG forum layout changed, or a real outage) -- the caller
    (`run_news_once`) treats that as a total fetch failure and reuses the
    previous publish (stale-while-error), same policy as before."""
    if game not in GGG_LISTING_URLS:
        raise ValueError(f"news: unknown game {game!r}")

    raw = _build_ggg_listing(http, game)
    if not raw:
        raise ValueError(f"news({game}): GGG forum listing scrape returned no threads")

    previous_by_id = {e["id"]: e for e in (previous_entries or [])}
    th_index = _build_th_link_index(http, game)

    entries: list[dict[str, Any]] = []
    for item in raw:
        thread_id = _extract_thread_id(item["link"])
        if thread_id is None:
            log.warning("news(%s): could not extract thread id from %r, skipping", game, item["link"])
            continue

        post_date_utc = _localize_to_utc(item["post_date"], item["tz_name"])
        if post_date_utc is None:
            log.warning("news(%s): could not parse post_date for %r, skipping", game, item["title"])
            continue

        entry_id = f"forum-{thread_id}"
        previous = previous_by_id.get(entry_id)
        if previous is not None:
            content = previous.get("content", "")
        else:
            try:
                content = fetch_thread_content(http, item["link"])
            except Exception:
                log.warning(
                    "news(%s): content scrape failed for new thread %r, publishing with empty content",
                    game, item["link"], exc_info=True,
                )
                content = ""

        category = "patch" if item["source_url"] in FORCED_PATCH_LISTING_URLS else categorize(item["title"])

        entry: dict[str, Any] = {
            "id": entry_id,
            "title": item["title"],
            "content": content,
            "category": category,
            "link": item["link"],
            "published": post_date_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        try:
            link_th = _resolve_th_link(th_index, item["post_date"]) if th_index else None
        except Exception:
            log.exception("news(%s): unexpected failure resolving TH link for %r", game, item["title"])
            link_th = None
        if link_th:
            entry["link_th"] = link_th

        entries.append(entry)

    entries.sort(key=lambda e: e["published"], reverse=True)
    return entries
