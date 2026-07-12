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
    publishes the standard www.pathofexile.com link, unconverted).
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
GGG_TITLE_LINK_RE = re.compile(r'<div class="title">\s*<a href="([^"]+)">\s*([^<]+?)\s*</a>', re.S)
# pathofexile.com returns 403 for a bare "Mozilla/5.0" (unlike Steam's API) --
# needs a full browser-looking UA. Confirmed live 2026-07-12.
GGG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

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


def _scrape_ggg_listing(http: HttpClient, url: str) -> list[tuple[str, str]]:
    text = http.get_text(url, headers=GGG_HEADERS)
    pairs = []
    for href, title in GGG_TITLE_LINK_RE.findall(text):
        link = href if href.startswith("http") else f"https://www.pathofexile.com{href}"
        pairs.append((link, _normalize_title(title)))
    return pairs


def _build_ggg_link_index(http: HttpClient, game: str) -> list[tuple[str, str]]:
    index: list[tuple[str, str]] = []
    for url in GGG_LISTING_URLS.get(game, []):
        try:
            index.extend(_scrape_ggg_listing(http, url))
        except Exception:
            log.warning("news(%s): failed to scrape %s for GGG links", game, url, exc_info=True)
    return index


def _resolve_ggg_link(index: list[tuple[str, str]], title: str) -> Optional[str]:
    query = _normalize_title(title)
    query_tokens = set(query.split())
    if not query_tokens:
        return None

    exact_links = {link for link, norm_title in index if norm_title == query}
    if len(exact_links) == 1:
        return next(iter(exact_links))
    if len(exact_links) > 1:
        log.warning("news: title %r matched %d GGG threads exactly, refusing to guess", title, len(exact_links))
        return None

    if len(query_tokens) < MIN_FUZZY_MATCH_TOKENS:
        return None

    fuzzy_links = {link for link, norm_title in index if query_tokens <= set(norm_title.split())}
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
    pointing at Steam, same graceful degradation as the bot repo's original."""
    entries = fetch_steam_news(http, game, failed_responses_dir)

    index = _build_ggg_link_index(http, game)
    for e in entries:
        try:
            ggg_link = _resolve_ggg_link(index, e["title"]) if index else None
        except Exception:
            log.exception("news(%s): unexpected failure resolving GGG link for %r, keeping Steam link", game, e["title"])
            continue
        if ggg_link:
            e["link"] = ggg_link
            e["link_is_ggg"] = True
        else:
            log.info("news(%s): no GGG listing match for %r, keeping Steam link", game, e["title"])

    return entries
