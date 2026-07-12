"""Single chokepoint for all outbound HTTP calls (spec sections 4 and 10.1a).

Every fetcher must go through HttpClient.get_json instead of calling
`requests` directly, so rate limiting, retry/backoff, and the structured
request log all apply uniformly.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import requests

from .rate_limiter import RateLimiter

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "poe-data-hub/0.1 (contact: foolshang.live@gmail.com)"
DEFAULT_FETCH_LOG_PATH = Path("data/fetch.log")


class HttpError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class HttpClient:
    def __init__(
        self,
        rate_limiter: RateLimiter,
        max_retries: int = 3,
        timeout_seconds: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
        fetch_log_path: Path = DEFAULT_FETCH_LOG_PATH,
    ):
        self._rate_limiter = rate_limiter
        self._max_retries = max_retries
        self._timeout = timeout_seconds
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._fetch_log_path = fetch_log_path
        self._fetch_log_path.parent.mkdir(parents=True, exist_ok=True)

    def get_json(self, url: str, *, headers: Optional[dict[str, str]] = None) -> Any:
        return self._get(url, headers=headers).json()

    def get_bytes(self, url: str, *, headers: Optional[dict[str, str]] = None) -> bytes:
        """Like get_json but for endpoints that return a binary body (e.g. poe.ninja's
        undocumented protobuf-ish builds-search API — see src/utils/pbwire.py)."""
        return self._get(url, headers=headers).content

    def get_text(self, url: str, *, headers: Optional[dict[str, str]] = None) -> str:
        """Like get_json but for HTML pages to scrape (e.g. GGG's forum
        listings and pathofexile.com's Deal of the Day sidebar — see
        src/fetchers/news.py, src/fetchers/deals.py)."""
        return self._get(url, headers=headers).text

    def _get(self, url: str, *, headers: Optional[dict[str, str]] = None) -> "requests.Response":
        host = urllib.parse.urlparse(url).netloc
        req_headers = {**self._headers, **(headers or {})}
        last_exc: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 1):
            self._rate_limiter.wait(host)
            start = time.monotonic()
            try:
                resp = requests.get(url, headers=req_headers, timeout=self._timeout)
            except requests.RequestException as e:
                last_exc = e
                self._log_request(host, url, None, start, attempt, "fail" if attempt >= self._max_retries else "retry")
                if attempt < self._max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise HttpError(f"{url} failed after {attempt} attempts: {e}") from e

            if resp.status_code == 429:
                self._log_request(host, url, 429, start, attempt, "retry")
                if attempt < self._max_retries:
                    retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                    time.sleep(retry_after if retry_after is not None else 2 ** attempt)
                    continue
                raise HttpError(f"{url} rate limited (429) after {attempt} attempts", 429)

            if resp.status_code >= 500:
                self._log_request(host, url, resp.status_code, start, attempt, "retry")
                if attempt < self._max_retries:
                    time.sleep(2 ** (attempt - 1))
                    continue
                raise HttpError(f"{url} server error {resp.status_code} after {attempt} attempts", resp.status_code)

            if resp.status_code >= 400:
                self._log_request(host, url, resp.status_code, start, attempt, "fail")
                raise HttpError(f"{url} client error {resp.status_code}", resp.status_code)

            self._log_request(host, url, resp.status_code, start, attempt, "ok")
            return resp

        raise HttpError(f"{url} failed after {self._max_retries} attempts: {last_exc}")

    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def _log_request(
        self, host: str, url: str, status_code: Optional[int], start: float, attempt: int, outcome: str
    ) -> None:
        entry = {
            "ts": time.time(),
            "host": host,
            "url": url,
            "status": status_code,
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
            "attempt": attempt,
            "outcome": outcome,
        }
        try:
            with self._fetch_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("could not write fetch log entry", exc_info=True)
