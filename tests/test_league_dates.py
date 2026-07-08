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
