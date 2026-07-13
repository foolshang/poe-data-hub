"""Detects the current PoE league from poe.ninja (spec section 3.6).

Ported from D:\\Projects\\Bot-Poe-Discord\\poe-discord-bot\\price.py, which
already solved the "which league is the real challenge league" problem:
poe.ninja's `economy/leagues` list is alphabetical, not "current first", and
an overlapping event league (e.g. "Return of the Ancestors") can look like a
real challenge league by item count alone. There is no API field that
disambiguates them, so a manual `league_override` is kept as an escape hatch
for that case only — it is not the primary detection path.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

LEAGUE_LIST_URL_TEMPLATE = "https://poe.ninja/{game}/api/economy/leagues"

# Exact token match, not substring: PoE2's hardcore variant is named
# "HC Runes of Aldur" (no "hardcore" substring), so a naive `in` check
# would miss it.
EXCLUDE_LEAGUE_TOKENS = {"hardcore", "hc", "standard", "ssf", "ruthless"}

# PoE1 and PoE2 use different HC naming conventions ("Hardcore Mirage" vs
# "HC Runes of Aldur") -- neither is hardcoded here. find_hardcore_variant()
# derives the HC name by token-matching against whatever the main league is
# called this cycle (phase 9), the same way main/event splitting already
# does token matching rather than hardcoding league names.
HARDCORE_TOKENS = {"hardcore", "hc"}

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")


@dataclass
class LeagueResult:
    main: str
    events: list[str] = field(default_factory=list)
    hardcore: Optional[str] = None


def find_hardcore_variant(main_league: str, all_league_names: list[str]) -> Optional[str]:
    """Find `main_league`'s Hardcore counterpart in the *unfiltered* league
    list (i.e. before EXCLUDE_LEAGUE_TOKENS strips HC/Standard/SSF/Ruthless
    out): a league name is the HC variant of `main_league` iff removing a
    hardcore token from its tokens leaves exactly `main_league`'s tokens.
    This correctly matches "HC Runes of Aldur" -> "Runes of Aldur" and
    "Hardcore Mirage" -> "Mirage", while correctly rejecting the permanent
    "Hardcore" league itself (no remaining tokens after stripping) and an HC
    variant of a *different* league, e.g. "Hardcore Ancestors" when
    main_league is "Mirage" (remaining tokens don't match). Returns None
    (never raises) if there's no match, or if more than one candidate
    matches ambiguously -- callers must treat both the same way: "no
    hardcore file this cycle", not an error."""
    main_tokens = set(main_league.lower().split())
    matches = []
    for name in all_league_names:
        tokens = set(name.lower().split())
        hc_tokens = tokens & HARDCORE_TOKENS
        if not hc_tokens:
            continue
        if tokens - hc_tokens == main_tokens:
            matches.append(name)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.warning(
            "league: %d ambiguous hardcore-variant candidates for main league %r: %r — publishing no hardcore file this cycle",
            len(matches), main_league, matches,
        )
    return None


class LeagueDetector:
    def __init__(
        self,
        http_client: HttpClient,
        game: str = "poe2",
        league_override: str = "",
        cache_ttl_seconds: float = 12 * 60 * 60,
        failed_responses_dir: Path = DEFAULT_FAILED_RESPONSES_DIR,
    ):
        self._http = http_client
        self._game = game
        self._override = (league_override or "").strip()
        self._ttl = cache_ttl_seconds
        self._failed_dir = failed_responses_dir
        self._failed_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Optional[tuple[float, LeagueResult]] = None

    def detect(self) -> LeagueResult:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < self._ttl:
            return self._cache[1]

        try:
            raw_names = self._fetch_raw_names()
            candidates = self._filter_candidates(raw_names)
            result = self._split_main_and_events(candidates, raw_names)
        except Exception as e:
            if self._cache:
                log.warning(
                    "league(%s): fetch failed (%s) — using stale cached leagues %r",
                    self._game, e, self._cache[1],
                )
                return self._cache[1]
            if self._override:
                log.warning(
                    "league(%s): fetch failed (%s) and no cache yet — falling back to league_override %r",
                    self._game, e, self._override,
                )
                return LeagueResult(main=self._override, events=[], hardcore=None)
            raise

        self._cache = (now, result)
        return result

    def _fetch_raw_names(self) -> list[str]:
        url = LEAGUE_LIST_URL_TEMPLATE.format(game=self._game)
        data = self._http.get_json(url)
        self._validate(data, url)

        names: list[str] = []
        for entry in data:
            name = entry.get("id") or entry.get("name") or ""
            if name:
                names.append(name)
        return names

    def _filter_candidates(self, raw_names: list[str]) -> list[str]:
        candidates = [name for name in raw_names if not (set(name.lower().split()) & EXCLUDE_LEAGUE_TOKENS)]
        if not candidates:
            raise ValueError(f"no eligible non-permanent league found in {raw_names!r}")
        return candidates

    def _validate(self, data: Any, url: str) -> None:
        if not isinstance(data, list) or len(data) == 0:
            self._dump_failed_response(data, url)
            raise ValueError(f"league list validation failed: expected non-empty list from {url}, got {type(data)!r}")

    def _dump_failed_response(self, data: Any, url: str) -> None:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        path = self._failed_dir / f"{host}_{int(time.time())}.json"
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.warning("could not write failed response dump", exc_info=True)

    def _split_main_and_events(self, candidates: list[str], raw_names: list[str]) -> LeagueResult:
        if self._override and self._override in candidates:
            main = self._override
        else:
            if self._override:
                log.warning(
                    "league(%s): configured league_override %r is not an active candidate %r — "
                    "guessing the first candidate is main (may be wrong during an overlapping event)",
                    self._game, self._override, candidates,
                )
            main = candidates[0]
        hardcore = find_hardcore_variant(main, raw_names)
        return LeagueResult(main=main, events=[c for c in candidates if c != main], hardcore=hardcore)
