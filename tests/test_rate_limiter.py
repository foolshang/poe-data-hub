"""Unit tests for src/utils/rate_limiter.py's per-host token-bucket wait()
and (2026-07-15) note_retry_after -- the shared-backoff broadcast added so
concurrent jobs/threads sharing one RateLimiter instance back off together
on a 429 instead of independently retrying into a longer ban (see
CLAUDE.md)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.utils.rate_limiter import RateLimiter


class _FakeClock:
    """Deterministic stand-in for time.monotonic/time.sleep, so these tests
    assert on RateLimiter's waiting *logic* instead of real elapsed
    wall-clock time -- real time.sleep()-based timing flaked occasionally
    on loaded CI runners (assertions like `>= 0.1` missing by a few ms)."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.now += seconds


@pytest.fixture
def clock():
    fake = _FakeClock()
    with patch("src.utils.rate_limiter.time.monotonic", fake.monotonic), \
         patch("src.utils.rate_limiter.time.sleep", fake.sleep):
        yield fake


def test_wait_enforces_interval_between_calls(clock):
    rl = RateLimiter(default_interval_seconds=0.1)
    start = clock.monotonic()
    rl.wait("host")
    rl.wait("host")
    assert clock.monotonic() - start >= 0.1


def test_per_host_interval_overrides_default(clock):
    rl = RateLimiter(default_interval_seconds=10.0, per_host={"fast.example.com": 0.0})
    start = clock.monotonic()
    rl.wait("fast.example.com")
    rl.wait("fast.example.com")
    assert clock.monotonic() - start < 1.0


def test_note_retry_after_delays_the_next_wait(clock):
    rl = RateLimiter(default_interval_seconds=0.0)
    rl.note_retry_after("host", 0.15)
    start = clock.monotonic()
    rl.wait("host")
    assert clock.monotonic() - start >= 0.15


def test_note_retry_after_does_not_shrink_an_already_longer_pause(clock):
    rl = RateLimiter(default_interval_seconds=0.0)
    rl.note_retry_after("host", 0.2)
    rl.note_retry_after("host", 0.05)  # shorter -- must not un-delay the host
    start = clock.monotonic()
    rl.wait("host")
    assert clock.monotonic() - start >= 0.2


def test_note_retry_after_only_affects_the_named_host(clock):
    rl = RateLimiter(default_interval_seconds=0.0)
    rl.note_retry_after("throttled.example.com", 0.2)
    start = clock.monotonic()
    rl.wait("other.example.com")
    assert clock.monotonic() - start < 0.1


def test_note_retry_after_is_visible_to_a_second_caller_sharing_the_instance(clock):
    """The whole point: one job learns of a 429+Retry-After, and a *different*
    job sharing the same RateLimiter instance immediately backs off too,
    without ever calling note_retry_after itself."""
    rl = RateLimiter(default_interval_seconds=0.0)

    rl.wait("host")  # job A's first request, unrelated
    rl.note_retry_after("host", 0.15)  # job A got 429'd

    start = clock.monotonic()
    rl.wait("host")  # job B, sharing the same limiter, never saw the 429
    assert clock.monotonic() - start >= 0.15
