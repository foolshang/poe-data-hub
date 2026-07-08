"""Token-bucket rate limiter, one bucket per host (spec section 4)."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, default_interval_seconds: float = 2.0, per_host: dict[str, float] | None = None):
        self._default = default_interval_seconds
        self._intervals = dict(per_host or {})
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def interval_for(self, host: str) -> float:
        return self._intervals.get(host, self._default)

    def wait(self, host: str) -> None:
        """Block until `host` is allowed to make its next request."""
        interval = self.interval_for(host)
        with self._lock:
            now = time.monotonic()
            earliest = self._next_allowed.get(host, now)
            start_at = max(now, earliest)
            self._next_allowed[host] = start_at + interval
        sleep_for = start_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
