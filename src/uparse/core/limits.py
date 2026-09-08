"""Per-host politeness: how many requests may be in flight, and how fast they may go.

Concurrency is a property of the *job*, but politeness is a property of the *host*. Ten
threads against ten sites is neighbourly; ten threads against one site is not, so the
limiter is keyed on host and the job-wide pool sits above it.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit


def host_of(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower() or "-"
    except ValueError:
        return "-"


class HostLimiter:
    """Bounds in-flight requests per host and spaces them by at least `delay_s`."""

    def __init__(self, per_host: int = 2, delay_s: float = 0.0) -> None:
        self.per_host = max(1, per_host)
        self.delay_s = max(0.0, delay_s)
        self._slots: dict[str, threading.Semaphore] = {}
        self._next_at: dict[str, float] = defaultdict(float)
        self._guard = threading.Lock()
        self._pace = threading.Lock()

    def _semaphore(self, host: str) -> threading.Semaphore:
        with self._guard:
            found = self._slots.get(host)
            if found is None:
                found = threading.Semaphore(self.per_host)
                self._slots[host] = found
            return found

    @contextmanager
    def hold(self, url: str, delay_s: float | None = None) -> Iterator[None]:
        host = host_of(url)
        semaphore = self._semaphore(host)
        semaphore.acquire()
        try:
            self._wait_turn(host, self.delay_s if delay_s is None else max(delay_s, self.delay_s))
            yield
        finally:
            semaphore.release()

    def _wait_turn(self, host: str, delay_s: float) -> None:
        """Claim this host's next slot, then sleep outside the lock so others can queue."""
        if delay_s <= 0:
            return
        with self._pace:
            now = time.monotonic()
            earliest = max(now, self._next_at[host])
            self._next_at[host] = earliest + delay_s
        remaining = earliest - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
