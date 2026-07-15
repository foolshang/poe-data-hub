"""Fetch PoE1/PoE2 news from Steam + resolve the real GGG thread link
(phase 8, ported from D:\\Projects\\Bot-Poe-Discord\\poe-discord-bot\\feeds.py
2026-07-05/2026-07-10 research -- see that file's history for how these
heuristics were found).

Two sources per game:
  GET https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/
      ?appid={appid}&count=15&maxlength=0&format=json
      -> the actual news items (title/body/id/published), filtered to
      feedname == "steam_community_announcements" only (other feednames on
      this endpoint are third-party press, not GGG's own posts).
  GET https://www.pathofexile.com/forum/view-forum/{...}
      -> scraped (regex, not an API) purely to build a title->thread-URL
      index, used to replace the generic Steam link with the real GGG forum
      thread when a confident match is found. Steam's own `contents` body
      text was tried first and rejected: only ~40-60% of posts actually
      embed a pathofexile(2).com URL in the body, and the ones that were
      present weren't even reliably correct (a real example matched an
      unrelated older thread by chance thread-id proximity). Title-based
      matching against the live listing page is what the bot repo settled
      on after that investigation, ported here unchanged.

Deliberately NOT ported from feeds.py (stays entirely in the bot repo):
  - seen.json / mark_seen / load_seen -- which entries have already been
    *posted* to Discord is bot-side state; the hub always returns the
    latest ~15 entries per game every run regardless of what's been
    consumed before (CLAUDE.md: state stays with the consumer).
  - detect_league_announcement() and the league-watch regexes -- an
    entirely different feature (auto-detecting league launch announcements
    to arm site-down watching), unrelated to what gets published here.
  - the www. -> th. domain rewrite -- explicitly a *display* concern for
    whichever consumer renders the link (approved decision: hub always
    publishes the standard www.pathofexile.com link, unconverted). `link_th`
    (below) is a different thing entirely -- a genuinely Thai-translated
    thread, not a mechanical domain swap of the English one.

Thai forum link (`link_th`, phase 11, from the bot repo's 2026-07-15 survey,
see its HISTORY.md): th.pathofexile.com carries **genuinely separate,
independently-translated forum threads** for PoE1's "news" category and
PoE2's categories `2282`/`2283` (Early Access Announcements / Patch Details
-- different forum ids than the English `2211`/`2212`; confirmed live
2026-07-15 that the English category numbers return English content
verbatim on th., not translations). Not every English thread has a Thai
counterpart (survey found 3/30 missing), and Steam's `published` timestamp
turned out to be UNRELIABLE for matching -- cross-checked live and found a
~7000-second gap between Steam's `date` and the *same* thread's own forum
post_date (different systems inside GGG, not a clean timezone offset).
What *does* line up tightly: the English GGG-forum thread's own post_date
against the Thai forum thread's own post_date -- both pages are the same
forum software, same display clock, and every real EN/TH pair found live
was within 60 seconds of each other (see SPEC.md for the actual measured
examples). So `link_th` is only attempted for entries that already resolved
a confident English GGG link (`link_is_ggg=True`) -- that English thread's
own post_date is the reference point matched against the Thai listing, not
Steam's `published`. Strict per the project owner's instruction: more than
one Thai candidate within tolerance is ambiguous, and ambiguous means no
`link_th` at all, never a guess -- a wrong Thai link is worse than none.
"""
from __future__ import annotations

import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")

STEAM_APPIDS: dict[str, int] = {"poe1": 238960, "poe2": 2694490}
STEAM_NEWS_URL_TEMPLATE = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count=15&maxlength=0&format=json"
)
OFFICIAL_FEEDNAME = "steam_community_announcements"
# Steam's API works fine with this bare UA (confirmed live 2026-07-12 from
# the same GCP VM that runs this job); no rate-limit/blocking concerns seen.
STEAM_HEADERS = {"User-Agent": "Mozilla/5.0"}

GGG_LISTING_URLS: dict[str, list[str]] = {
    "poe1": ["https://www.pathofexile.com/forum/view-forum/news"],
    "poe2": [
        "https://www.pathofexile.com/forum/view-forum/2211",  # announcements
        "https://www.pathofexile.com/forum/view-forum/2212",  # patch notes
    ],
}
GGG_WWW_DOMAIN = "https://www.pathofexile.com"
# Captures (href, title, "<postBy post_date span content>") in one pass --
# the post_date is the thread's *own* creation time (from its "postBy" line,
# not "last_post" which can be a later reply) and is needed for phase 11's
# Thai-link time-proximity matching, not just the link/title phase 8 used.
GGG_TITLE_LINK_DATE_RE = re.compile(
    r'<div class="title">\s*<a href="([^"]+)">\s*([^<]+?)\s*</a>.*?<span class="post_date">,\s*([^<]+)</span>',
    re.S,
)
# pathofexile.com returns 403 for a bare "Mozilla/5.0" (unlike Steam's API) --
# needs a full browser-looking UA. Confirmed live 2026-07-12.
GGG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Thai forum categories are NOT the same forum ids as English (phase 11,
# confirmed live 2026-07-15) -- th.pathofexile.com on English PoE2's 2211/2212
# returns English content verbatim (just Thai UI chrome), not translations.
# Real Thai PoE2 categories: 2282 = "Early Access Announcements" (th. name:
# "ประกาศ Early Access"), 2283 = "Patch Details" ("รายละเอียดแพทช์"). PoE1's
# "news" category *is* the same URL path on th. (genuinely translated there).
TH_GGG_LISTING_URLS: dict[str, list[str]] = {
    "poe1": ["https://th.pathofexile.com/forum/view-forum/news"],
    "poe2": [
        "https://th.pathofexile.com/forum/view-forum/2282",
        "https://th.pathofexile.com/forum/view-forum/2283",
    ],
}
TH_DOMAIN = "https://th.pathofexile.com"

# RTGS-standard Thai month abbreviations as used on th.pathofexile.com
# (confirmed live 2026-07-15 for "มิ.ย."/"ก.ค." against real thread dates;
# the rest follow the same well-established convention).
_TH_MONTH_ABBR: dict[str, int] = {
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}
# e.g. "15 ก.ค. 2026 10:10:13" (day, Thai month abbr, year, 24h time -- no
# comma separators, unlike the English "Jul 10, 2026, 4:00:00 AM" style).
_TH_POST_DATE_RE = re.compile(r"(\d{1,2})\s+(\S+)\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})")

# Empirically strict on purpose (project owner: "จับคู่ไม่มั่นใจ = ไม่ใส่ field
# เด็ดขาด" -- uncertain match = never include the field). Every real EN<->TH
# pair found live 2026-07-15 (both games) was within 60 seconds of each
# other -- 300s leaves generous headroom above every observed real gap while
# staying far tighter than "same day". See SPEC.md for the actual examples.
TH_MATCH_TOLERANCE_SECONDS = 300

TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
# Fuzzy (token-subset) matching only kicks in for reasonably specific titles --
# a short query like "Patch Notes" would trivially subset-match nearly every
# patch thread ever posted, and containment alone can't disambiguate
# near-duplicates like "0.5.4b Hotfix 2" vs "...Hotfix 3".
MIN_FUZZY_MATCH_TOKENS = 4

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


def _dump_failed_response(data: Any, url: str, label: str, failed_dir: Path) -> None:
    failed_dir.mkdir(parents=True, exist_ok=True)
    path = failed_dir / f"{label}_{int(time.time())}.json"
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.warning("news: could not write failed response dump", exc_info=True)


def fetch_steam_news(
    http: HttpClient, game: str, failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR
) -> list[dict[str, Any]]:
    if game not in STEAM_APPIDS:
        raise ValueError(f"news: unknown game {game!r}")
    url = STEAM_NEWS_URL_TEMPLATE.format(appid=STEAM_APPIDS[game])
    data = http.get_json(url, headers=STEAM_HEADERS)

    items = (data.get("appnews") or {}).get("newsitems") if isinstance(data, dict) else None
    if not isinstance(items, list):
        _dump_failed_response(data, url, f"steam_news_{game}", failed_responses_dir)
        raise ValueError(f"news({game}): response validation failed for Steam News ({url})")

    entries: list[dict[str, Any]] = []
    for item in items:
        if item.get("feedname") != OFFICIAL_FEEDNAME:
            continue
        title = item["title"]
        entries.append(
            {
                "id": str(item["gid"]),
                "title": title,
                "content": clean_content(item.get("contents", "")),
                "category": categorize(title),
                "link": item["url"],
                "link_is_ggg": False,
                "published": datetime.fromtimestamp(item["date"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    return entries


def _normalize_title(title: str) -> str:
    text = TITLE_NORMALIZE_RE.sub(" ", html.unescape(title).casefold())
    return " ".join(text.split())


def _parse_en_post_date(text: str) -> Optional[datetime]:
    """Parses the English forum's "Jul 10, 2026, 4:00:00 AM" style post_date
    text. Naive datetime on purpose (no tzinfo) -- only ever compared against
    another naive datetime from the *same* forum display clock (see
    _resolve_th_link), never treated as an absolute/UTC time."""
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


def _scrape_listing_with_dates(
    http: HttpClient, url: str, domain: str, parse_date_fn,
) -> list[tuple[str, str, Optional[datetime]]]:
    text = http.get_text(url, headers=GGG_HEADERS)
    entries = []
    for href, title, date_text in GGG_TITLE_LINK_DATE_RE.findall(text):
        link = href if href.startswith("http") else f"{domain}{href}"
        entries.append((link, _normalize_title(title), parse_date_fn(date_text)))
    return entries


def _scrape_ggg_listing(http: HttpClient, url: str) -> list[tuple[str, str, Optional[datetime]]]:
    return _scrape_listing_with_dates(http, url, GGG_WWW_DOMAIN, _parse_en_post_date)


def _scrape_th_listing(http: HttpClient, url: str) -> list[tuple[str, str, Optional[datetime]]]:
    return _scrape_listing_with_dates(http, url, TH_DOMAIN, _parse_th_post_date)


def _build_ggg_link_index(http: HttpClient, game: str) -> list[tuple[str, str, Optional[datetime]]]:
    index: list[tuple[str, str, Optional[datetime]]] = []
    for url in GGG_LISTING_URLS.get(game, []):
        try:
            index.extend(_scrape_ggg_listing(http, url))
        except Exception:
            log.warning("news(%s): failed to scrape %s for GGG links", game, url, exc_info=True)
    return index


def _build_th_link_index(http: HttpClient, game: str) -> list[tuple[str, str, Optional[datetime]]]:
    index: list[tuple[str, str, Optional[datetime]]] = []
    for url in TH_GGG_LISTING_URLS.get(game, []):
        try:
            index.extend(_scrape_th_listing(http, url))
        except Exception:
            log.warning("news(%s): failed to scrape %s for TH links", game, url, exc_info=True)
    return index


def _resolve_th_link(th_index: list[tuple[str, str, Optional[datetime]]], en_post_date: Optional[datetime]) -> Optional[str]:
    """Strict time-proximity match: a Thai thread is a confident match only
    if it's the *single* Thai candidate within TH_MATCH_TOLERANCE_SECONDS of
    the English thread's own post_date. Two or more candidates within
    tolerance is ambiguous -> None, same "refuse to guess" policy
    _resolve_ggg_link already uses for title matches."""
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


def _resolve_ggg_link(index: list[tuple[str, str, Optional[datetime]]], title: str) -> Optional[str]:
    query = _normalize_title(title)
    query_tokens = set(query.split())
    if not query_tokens:
        return None

    exact_links = {link for link, norm_title, _post_date in index if norm_title == query}
    if len(exact_links) == 1:
        return next(iter(exact_links))
    if len(exact_links) > 1:
        log.warning("news: title %r matched %d GGG threads exactly, refusing to guess", title, len(exact_links))
        return None

    if len(query_tokens) < MIN_FUZZY_MATCH_TOKENS:
        return None

    fuzzy_links = {link for link, norm_title, _post_date in index if query_tokens <= set(norm_title.split())}
    if len(fuzzy_links) == 1:
        return next(iter(fuzzy_links))
    if len(fuzzy_links) > 1:
        log.info("news: title %r fuzzy-matched %d different GGG threads, ambiguous -- keeping Steam link", title, len(fuzzy_links))

    return None


def fetch_news(http: HttpClient, game: str, failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR) -> list[dict[str, Any]]:
    """Steam news for `game`, each entry's `link` upgraded to the real GGG
    forum thread (www.pathofexile.com -- never rewritten to th., that's a
    display concern for whoever renders it) when a confident title match is
    found; otherwise the entry keeps its original Steam link. A GGG-listing
    scrape failure never drops an entry -- worst case every entry just keeps
    pointing at Steam, same graceful degradation as the bot repo's original.

    `link_th` (phase 11, optional/additive) is only attempted once an entry
    already has a confident `link_is_ggg=True` English match -- that English
    thread's own post_date (not Steam's `published`, see module docstring)
    is matched against the Thai listing by time proximity. A TH-listing
    scrape failure, or no confident match, simply leaves `link_th` absent --
    never a guess, never a reason to touch the English `link`."""
    entries = fetch_steam_news(http, game, failed_responses_dir)

    index = _build_ggg_link_index(http, game)
    en_post_date_by_link = {link: post_date for link, _title, post_date in index}
    th_index = _build_th_link_index(http, game)

    for e in entries:
        try:
            ggg_link = _resolve_ggg_link(index, e["title"]) if index else None
        except Exception:
            log.exception("news(%s): unexpected failure resolving GGG link for %r, keeping Steam link", game, e["title"])
            continue
        if not ggg_link:
            log.info("news(%s): no GGG listing match for %r, keeping Steam link", game, e["title"])
            continue

        e["link"] = ggg_link
        e["link_is_ggg"] = True

        try:
            link_th = _resolve_th_link(th_index, en_post_date_by_link.get(ggg_link)) if th_index else None
        except Exception:
            log.exception("news(%s): unexpected failure resolving TH link for %r, leaving link_th absent", game, e["title"])
            continue
        if link_th:
            e["link_th"] = link_th

    return entries
