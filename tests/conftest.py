"""Shared pytest fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_shared_rate_limiter():
    """src.main._build_http_client caches one RateLimiter per process
    (2026-07-15 fix, see CLAUDE.md) so concurrent jobs share 429/Retry-After
    backoff state. That module-level singleton would otherwise leak between
    tests -- reset it before and after every test so each test's `cfg`
    (rate_limits, etc.) is honored independently."""
    import src.main as main_module

    main_module._shared_rate_limiter = None
    yield
    main_module._shared_rate_limiter = None
