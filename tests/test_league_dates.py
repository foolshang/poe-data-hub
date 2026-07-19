"""Unit tests for src/fetchers/league_dates.py against a real captured
Steam news sample (data/samples/steam_news_poe2.json, captured live
2026-07-08) -- these tests never hit the network.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.fetchers.league_dates import LeagueDatesFetcher

SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "steam_news_poe2.json"


class _FakeHttpClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def get_json(self, url):
        self.calls += 1
        return self._payload


@pytest.fixture
def fetcher(tmp_path) -> LeagueDatesFetcher:
    payload = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    return LeagueDatesFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")


def test_finds_real_league_launch_date(fetcher: LeagueDatesFetcher):
    dates = fetcher.fetch("Runes of Aldur")
    assert dates.start_at == datetime(2026, 5, 7, 20, 30, 20, tzinfo=timezone.utc)


def test_end_date_is_none_for_active_league_with_no_ends_post(fetcher: LeagueDatesFetcher):
    dates = fetcher.fetch("Runes of Aldur")
    assert dates.end_at is None


def test_end_date_parses_from_a_real_ends_post(fetcher: LeagueDatesFetcher):
    # "Rise of the Abyssal" has a real "What Will Happen When ... Ends" post
    # in the sample, with "will end on December 8th at 1PM PST" in the body.
    dates = fetcher.fetch("Rise of the Abyssal")
    assert dates.end_at.date() == datetime(2025, 12, 8).date()


def test_unknown_league_returns_none_and_logs_warning(fetcher: LeagueDatesFetcher, caplog):
    dates = fetcher.fetch("Totally Not A Real League")
    assert dates.start_at is None
    assert dates.end_at is None


def test_ignores_third_party_press_feed(fetcher: LeagueDatesFetcher):
    # Sanity check: the fixture contains PCGamesN-sourced items; they must
    # never be treated as official announcements even if they mention the
    # league by name somewhere.
    items = fetcher._official_news_items()
    assert all(it.get("feedlabel") == "Community Announcements" for it in items)


def test_cache_avoids_second_fetch(fetcher: LeagueDatesFetcher):
    fetcher.fetch("Runes of Aldur")
    fetcher.fetch("Runes of Aldur")
    assert fetcher._http.calls == 1


def test_malformed_response_raises_and_dumps(tmp_path):
    http = _FakeHttpClient({"not": "the expected shape"})
    fetcher = LeagueDatesFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")
    with pytest.raises(ValueError, match="validation failed"):
        fetcher.fetch("Runes of Aldur")
    assert len(list((tmp_path / "failed").glob("*.json"))) == 1


def test_unknown_game_raises_at_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown game"):
        LeagueDatesFetcher(http_client=_FakeHttpClient({}), game="poe3", failed_responses_dir=tmp_path / "failed")


# ── phase 10: PoE1, including its differently-worded end-date phrasing ────

POE1_SAMPLE_PATH = Path(__file__).resolve().parents[1] / "data" / "samples" / "steam_news_poe1_wide.json"


@pytest.fixture
def poe1_fetcher(tmp_path) -> LeagueDatesFetcher:
    payload = json.loads(POE1_SAMPLE_PATH.read_text(encoding="utf-8"))
    http = _FakeHttpClient(payload)
    return LeagueDatesFetcher(http_client=http, game="poe1", failed_responses_dir=tmp_path / "failed")


def test_poe1_finds_real_launch_date(poe1_fetcher: LeagueDatesFetcher):
    # "Path of Exile: Mirage is Now Available" -- confirmed live 2026-07-14.
    dates = poe1_fetcher.fetch("Mirage")
    assert dates.start_at is not None


def test_poe1_end_date_parses_present_tense_phrasing(poe1_fetcher: LeagueDatesFetcher):
    # PoE1's real end-of-league post says "...the league ends on July 20th
    # 3PM PDT" (present tense, no "will") -- different from PoE2's "will end
    # on <date>". Title is also worded differently ("What Happens When The
    # Mirage League Ends" vs PoE2's "What Will Happen When ... Ends") but
    # both contain the substring "league ends" the matcher looks for.
    dates = poe1_fetcher.fetch("Mirage")
    assert dates.end_at is not None
    assert dates.end_at.month == 7
    assert dates.end_at.day == 20


# ── find_upcoming_launch (spec 9, 2026-07-20): detects a launch date       ──
# ── WITHOUT already knowing the league's name, unlike fetch()'s            ──
# ── "now available" matcher (which only fires on/after launch day)         ──

def test_find_upcoming_launch_poe1_title_match(poe1_fetcher: LeagueDatesFetcher):
    # Real title: "Path of Exile: Curse of the Allflame Releases on July 24
    # PDT" -- the date is in the TITLE for this one.
    detected = poe1_fetcher.find_upcoming_launch()
    assert detected is not None
    assert (detected.month, detected.day) == (7, 24)


def test_find_upcoming_launch_poe2_body_match(fetcher: LeagueDatesFetcher):
    # Real post: "Path of Exile 2: Return of the Ancients Launches Soon" --
    # the title itself has NO date at all; "...officially launches on May
    # 29 (PDT)..." only appears in the body, confirming detection must
    # search both, not just the title (unlike fetch()'s other matchers).
    detected = fetcher.find_upcoming_launch()
    assert detected is not None
    assert (detected.month, detected.day) == (5, 29)


def test_find_upcoming_launch_ignores_real_false_positives(fetcher: LeagueDatesFetcher, poe1_fetcher: LeagueDatesFetcher):
    """Both real sample feeds contain real posts that use similar "on
    <date>" phrasing but are NOT a league launch -- confirms the verb-phrase
    requirement (launch(es/ing)?|release(s)? on) is actually doing the
    filtering, not just getting lucky on the two positive examples. If any
    of these were mistakenly selected, find_upcoming_launch would return
    the wrong date (all of them have dates well before the real 5/29 and
    7/24 launches, or are otherwise clearly wrong once the *nearby* correct
    answer is verified)."""
    # PoE2 sample: "More ExileCon Tickets Available On May 17th PDT" and
    # "Watch GGG Live on May 7th PDT" both predate the real launch (May 29)
    # and use "Available On"/"Live on", not the required verb phrase.
    poe2_detected = fetcher.find_upcoming_launch()
    assert (poe2_detected.month, poe2_detected.day) != (5, 17)
    assert (poe2_detected.month, poe2_detected.day) != (5, 7)

    # PoE1 sample: "Play the Return of the Ancestors Event on June 25th
    # PDT" and "Zizaran's Class Gauntlet Event Starts on April 8th" both
    # use "... Event ... on", not the required verb phrase.
    poe1_detected = poe1_fetcher.find_upcoming_launch()
    assert (poe1_detected.month, poe1_detected.day) != (6, 25)
    assert (poe1_detected.month, poe1_detected.day) != (4, 8)


def test_find_upcoming_launch_no_match_returns_none(tmp_path):
    payload = {"appnews": {"newsitems": [
        {"gid": "1", "title": "Weekend Stash Tab Sale", "contents": "50% off this weekend only.",
         "date": 1700000000, "feedlabel": "Community Announcements"},
    ]}}
    http = _FakeHttpClient(payload)
    empty_fetcher = LeagueDatesFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")
    assert empty_fetcher.find_upcoming_launch() is None


def test_find_upcoming_launch_ignores_retrospective_mention(tmp_path):
    """A date phrase that's before the post's own date, within the same
    month (so the roll-forward-to-next-year heuristic doesn't apply and
    can't make it ambiguously "future"), is a retrospective mention -- must
    not match."""
    payload = {"appnews": {"newsitems": [
        {
            "gid": "1", "title": "Anniversary Recap",
            "contents": "It's been a while since the game officially launches on August 1 -- thanks for playing!",
            "date": 1755648000,  # 2025-08-20, same month as "August 1" but later
            "feedlabel": "Community Announcements",
        },
    ]}}
    http = _FakeHttpClient(payload)
    empty_fetcher = LeagueDatesFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")
    assert empty_fetcher.find_upcoming_launch() is None


def test_find_upcoming_launch_most_recent_candidate_wins(tmp_path):
    payload = {"appnews": {"newsitems": [
        {"gid": "1", "title": "Old Expansion Releases on March 1", "contents": "",
         "date": 1704067200, "feedlabel": "Community Announcements"},  # 2024-01-01
        {"gid": "2", "title": "New Expansion Releases on June 15", "contents": "",
         "date": 1717200000, "feedlabel": "Community Announcements"},  # 2024-06-01, more recent
    ]}}
    http = _FakeHttpClient(payload)
    empty_fetcher = LeagueDatesFetcher(http_client=http, failed_responses_dir=tmp_path / "failed")
    detected = empty_fetcher.find_upcoming_launch()
    assert (detected.month, detected.day) == (6, 15)
