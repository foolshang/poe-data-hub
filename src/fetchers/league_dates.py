"""Fetch PoE1/PoE2 league start/end dates from Steam news (spec 6.4/phase-6
addition for `days_since_league_start` and `endAt`; PoE1 support added
phase 10).

GGG's own `pathofexile.com/api/leagues` doesn't support PoE2 -- the `realm`
query param is silently ignored and it always returns PoE1 ("pc") data,
verified live 2026-07-08 with several param values including a bogus one.
poe2db.tw has a structured countdown timer on its homepage, but it measures
something other than league start: for Runes of Aldur it showed
2026-05-29, while GGG's own Steam post explicitly announcing the league is
dated 2026-05-07 (confirmed by reading the post body, which names "Runes of
Aldur" directly) -- so poe2db was not used. (PoE1's `pathofexile.com/api/leagues`
does work in principle, but wasn't used here either -- Steam news already
solved this uniformly for PoE2 and reusing the same mechanism for both games
means one code path instead of two, and it's still not GGG, so the "GGG in
exactly one place" rule stays satisfied for both games too.)

Instead this hits the public Steam news API (not GGG, doesn't touch the
"GGG in exactly one place" rule) and looks for two GGG announcement
patterns, restricted to `feedlabel == "Community Announcements"` to exclude
syndicated third-party press (PCGamesN etc, which show up in the same feed
and don't reliably state dates):

  - Launch: title contains "now available" AND body names the league
    explicitly (GGG's launch-day posts always name both the expansion and
    the new challenge league, e.g. "Our Biggest Expansion Now Available"
    naming "Runes of Aldur" in the body even though the title doesn't).
    -> that post's own `date` (unix epoch) is startAt. Confirmed working
    unchanged for PoE1 live 2026-07-14: "Path of Exile: Mirage is Now
    Available" names "Mirage" in the body.
  - End: title contains "league ends" (matches both PoE2's "What Will
    Happen When the <league> League Ends" and PoE1's differently-worded
    "What Happens When The <league> League Ends" -- both contain that
    substring) and body contains a "<verb> on <Month> <Day>[ at ...]"
    sentence, parsed best-effort via dateutil. **PoE1's body phrasing is
    "ends on ..." (present tense), not PoE2's "will end on ..." (future
    tense)** -- discovered live 2026-07-14 from Mirage's real end-of-league
    post ("...here's what you can expect when the league ends on July
    20th 3PM PDT"), which the original PoE2-only regex would have silently
    failed to match. `_END_DATE_RE` below matches both by only requiring
    "end(s) on", not "will end on". This post normally doesn't exist until
    GGG announces the *next* league, so endAt is null for most of a
    league's life -- that's expected, not a bug.

Both lookups are best-effort: no match, or a match that fails to parse,
returns None and logs a warning. Never raises for a missing/unparseable
date -- only for a wholly malformed Steam API response (spec 10.1b).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dateutil import parser as dateutil_parser

from src.fetchers.news import STEAM_APPIDS
from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/"
OFFICIAL_FEED_LABEL = "Community Announcements"

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")

# Matches both "will end on <Month> <Day>" (PoE2's phrasing) and "ends on
# <Month> <Day>" (PoE1's phrasing, present tense, no "will") -- see module
# docstring. "will end on X" already contains the substring "end on X", so
# this single pattern (no "will" requirement) covers both without branching.
_END_DATE_RE = re.compile(
    r"ends? on ([A-Za-z]+ \d{1,2})(?:st|nd|rd|th)?", re.IGNORECASE
)


@dataclass(frozen=True)
class LeagueDates:
    start_at: Optional[datetime]
    end_at: Optional[datetime]


class LeagueDatesFetcher:
    def __init__(
        self,
        http_client: HttpClient,
        game: str = "poe2",
        cache_ttl_seconds: float = 12 * 60 * 60,
        failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR,
    ):
        if game not in STEAM_APPIDS:
            raise ValueError(f"league_dates: unknown game {game!r}")
        self._http = http_client
        self._game = game
        self._appid = STEAM_APPIDS[game]
        self._ttl = cache_ttl_seconds
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[tuple[float, list[dict[str, Any]]]] = None

    def fetch(self, league_name: str) -> LeagueDates:
        items = self._official_news_items()
        start_at = self._find_start(items, league_name)
        end_at = self._find_end(items, league_name)
        if start_at is None:
            log.warning("league_dates(%s): no launch announcement found for league %r on Steam news", self._game, league_name)
        return LeagueDates(start_at=start_at, end_at=end_at)

    def _official_news_items(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self._ttl:
            return self._cache[1]

        url = f"{STEAM_NEWS_URL}?appid={self._appid}&count=100&maxlength=3000&format=json"
        data = self._http.get_json(url)
        self._validate(data, url)

        items = [
            it for it in data["appnews"]["newsitems"]
            if it.get("feedlabel") == OFFICIAL_FEED_LABEL
        ]
        self._cache = (now, items)
        return items

    def _validate(self, data: Any, url: str) -> None:
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("appnews"), dict)
            or not isinstance(data["appnews"].get("newsitems"), list)
        ):
            self._dump_failed_response(data, url)
            raise ValueError(f"league_dates: Steam news response validation failed: {url}")

    def _find_start(self, items: list[dict[str, Any]], league_name: str) -> Optional[datetime]:
        candidates = [
            it for it in items
            if "now available" in it.get("title", "").lower()
            and league_name.lower() in it.get("contents", "").lower()
        ]
        if not candidates:
            return None
        best = min(candidates, key=lambda it: it["date"])
        return datetime.fromtimestamp(best["date"], tz=timezone.utc)

    def _find_end(self, items: list[dict[str, Any]], league_name: str) -> Optional[datetime]:
        candidates = [
            it for it in items
            if "league ends" in it.get("title", "").lower()
            and league_name.lower() in it.get("title", "").lower()
        ]
        if not candidates:
            return None
        post = max(candidates, key=lambda it: it["date"])
        match = _END_DATE_RE.search(post.get("contents", ""))
        if not match:
            log.warning("league_dates: found an 'ends' post for %r but couldn't parse a date from it", league_name)
            return None
        post_dt = datetime.fromtimestamp(post["date"], tz=timezone.utc)
        try:
            parsed = dateutil_parser.parse(match.group(1), default=post_dt)
        except (ValueError, OverflowError):
            log.warning("league_dates: unparseable end date %r for %r", match.group(1), league_name)
            return None
        if parsed.month < post_dt.month:
            parsed = parsed.replace(year=post_dt.year + 1)
        return parsed.replace(tzinfo=timezone.utc)

    def _dump_failed_response(self, data: Any, url: str) -> None:
        import json

        host = url.split("//", 1)[-1].split("/", 1)[0]
        path = self._failed_dir / f"{host}_{int(time.time())}.json"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("league_dates: could not write failed response dump", exc_info=True)
