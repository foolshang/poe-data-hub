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

DEFAULT_FAILED_RESPONSES_DIR = Path("data/failed_responses")


@dataclass
class LeagueResult:
    main: str
    events: list[str] = field(default_factory=list)


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
            candidates = self._fetch_candidates()
            result = self._split_main_and_events(candidates)
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
                return LeagueResult(main=self._override, events=[])
            raise

        self._cache = (now, result)
        return result

    def _fetch_candidates(self) -> list[str]:
        url = LEAGUE_LIST_URL_TEMPLATE.format(game=self._game)
        data = self._http.get_json(url)
        self._validate(data, url)

        candidates: list[str] = []
        for entry in data:
            name = entry.get("id") or entry.get("name") or ""
            if not name:
                continue
            tokens = set(name.lower().split())
            if tokens & EXCLUDE_LEAGUE_TOKENS:
                continue
            candidates.append(name)

        if not candidates:
            raise ValueError(f"no eligible non-permanent league found in {data!r}")
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

    def _split_main_and_events(self, candidates: list[str]) -> LeagueResult:
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
        return LeagueResult(main=main, events=[c for c in candidates if c != main])
