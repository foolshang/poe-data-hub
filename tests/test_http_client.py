"""Unit tests for src/utils/http_client.py's retry/backoff behavior (DoD:
"โดน 429/timeout แล้วระบบ backoff เองโดยไม่ crash", spec section 4)."""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
import requests

from src.utils.http_client import HttpClient, HttpError
from src.utils.rate_limiter import RateLimiter


@pytest.fixture
def client(tmp_path) -> HttpClient:
    rl = RateLimiter(default_interval_seconds=0.0)
    return HttpClient(rl, max_retries=3, timeout_seconds=5, fetch_log_path=tmp_path / "fetch.log")


def _resp(status_code: int, json_body=None, headers=None):
    r = Mock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    r.headers = headers or {}
    return r


def test_429_retries_then_succeeds(client: HttpClient):
    responses = [_resp(429, headers={"Retry-After": "0"}), _resp(200, {"ok": True})]
    with patch("requests.get", side_effect=responses) as mock_get, patch("time.sleep"):
        result = client.get_json("https://example.com/x")
    assert result == {"ok": True}
    assert mock_get.call_count == 2


def test_429_exhausts_retries_raises_http_error(client: HttpClient):
    responses = [_resp(429, headers={"Retry-After": "0"})] * 3
    with patch("requests.get", side_effect=responses), patch("time.sleep"):
        with pytest.raises(HttpError, match="rate limited"):
            client.get_json("https://example.com/x")


def test_connection_error_retries_then_succeeds(client: HttpClient):
    side_effects = [requests.ConnectionError("boom"), _resp(200, {"ok": True})]
    with patch("requests.get", side_effect=side_effects), patch("time.sleep"):
        result = client.get_json("https://example.com/x")
    assert result == {"ok": True}


def test_connection_error_exhausts_retries_raises_http_error(client: HttpClient):
    with patch("requests.get", side_effect=requests.ConnectionError("boom")), patch("time.sleep"):
        with pytest.raises(HttpError, match="failed after"):
            client.get_json("https://example.com/x")


def test_5xx_retries_then_succeeds(client: HttpClient):
    responses = [_resp(503), _resp(200, {"ok": True})]
    with patch("requests.get", side_effect=responses), patch("time.sleep"):
        result = client.get_json("https://example.com/x")
    assert result == {"ok": True}


def test_4xx_client_error_does_not_retry(client: HttpClient):
    with patch("requests.get", return_value=_resp(404)) as mock_get, patch("time.sleep"):
        with pytest.raises(HttpError, match="client error 404"):
            client.get_json("https://example.com/x")
    assert mock_get.call_count == 1


def test_get_bytes_returns_raw_content(client: HttpClient):
    r = _resp(200)
    r.content = b"\x01\x02\x03"
    with patch("requests.get", return_value=r), patch("time.sleep"):
        result = client.get_bytes("https://example.com/x")
    assert result == b"\x01\x02\x03"
