"""Unit tests for src/utils/rate_limiter.py's per-host token-bucket wait()
and (2026-07-15) note_retry_after -- the shared-backoff broadcast added so
concurrent jobs/threads sharing one RateLimiter instance back off together
on a 429 instead of independently retrying into a longer ban (see
CLAUDE.md)."""
from __future__ import annotations

import time

from src.utils.rate_limiter import RateLimiter


def test_wait_enforces_interval_between_calls():
    rl = RateLimiter(default_interval_seconds=0.1)
    start = time.monotonic()
    rl.wait("host")
    rl.wait("host")
    assert time.monotonic() - start >= 0.1


def test_per_host_interval_overrides_default():
    rl = RateLimiter(default_interval_seconds=10.0, per_host={"fast.example.com": 0.0})
    start = time.monotonic()
    rl.wait("fast.example.com")
    rl.wait("fast.example.com")
    assert time.monotonic() - start < 1.0


def test_note_retry_after_delays_the_next_wait():
    rl = RateLimiter(default_interval_seconds=0.0)
    rl.note_retry_after("host", 0.15)
    start = time.monotonic()
    rl.wait("host")
    # small tolerance: Windows dev-machine timer resolution can return
    # time.sleep() a few ms early; production only runs on Linux.
    assert time.monotonic() - start >= 0.13


def test_note_retry_after_does_not_shrink_an_already_longer_pause():
    rl = RateLimiter(default_interval_seconds=0.0)
    rl.note_retry_after("host", 0.2)
    rl.note_retry_after("host", 0.05)  # shorter -- must not un-delay the host
    start = time.monotonic()
    rl.wait("host")
    assert time.monotonic() - start >= 0.18


def test_note_retry_after_only_affects_the_named_host():
    rl = RateLimiter(default_interval_seconds=0.0)
    rl.note_retry_after("throttled.example.com", 0.2)
    start = time.monotonic()
    rl.wait("other.example.com")
    assert time.monotonic() - start < 0.1


def test_note_retry_after_is_visible_to_a_second_caller_sharing_the_instance():
    """The whole point: one job learns of a 429+Retry-After, and a *different*
    job sharing the same RateLimiter instance immediately backs off too,
    without ever calling note_retry_after itself."""
    rl = RateLimiter(default_interval_seconds=0.0)

    rl.wait("host")  # job A's first request, unrelated
    rl.note_retry_after("host", 0.15)  # job A got 429'd

    start = time.monotonic()
    rl.wait("host")  # job B, sharing the same limiter, never saw the 429
    assert time.monotonic() - start >= 0.13  # tolerance: see note above
