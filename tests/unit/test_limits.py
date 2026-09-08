"""Per-host limits: concurrency is job-wide, politeness is per host."""

from __future__ import annotations

import threading
import time

from uparse.core.limits import HostLimiter, host_of


def test_host_of_reads_the_netloc():
    assert host_of("https://Example.COM/a/b?c=1") == "example.com"
    assert host_of("not a url") == "-"


def test_requests_to_one_host_are_spaced_by_the_delay():
    limiter = HostLimiter(per_host=4, delay_s=0.05)
    started = time.monotonic()
    for _ in range(3):
        with limiter.hold("https://one.test/page"):
            pass
    assert time.monotonic() - started >= 0.10


def test_different_hosts_do_not_wait_for_each_other():
    limiter = HostLimiter(per_host=1, delay_s=0.05)
    started = time.monotonic()
    for host in ("a", "b", "c"):
        with limiter.hold(f"https://{host}.test/page"):
            pass
    assert time.monotonic() - started < 0.10


def test_only_per_host_requests_run_at_once():
    limiter = HostLimiter(per_host=2, delay_s=0.0)
    live = 0
    peak = 0
    guard = threading.Lock()

    def visit() -> None:
        nonlocal live, peak
        with limiter.hold("https://one.test/page"):
            with guard:
                live += 1
                peak = max(peak, live)
            time.sleep(0.02)
            with guard:
                live -= 1

    threads = [threading.Thread(target=visit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak <= 2


def test_a_longer_crawl_delay_wins_over_the_configured_one():
    limiter = HostLimiter(per_host=1, delay_s=0.0)
    started = time.monotonic()
    for _ in range(2):
        with limiter.hold("https://slow.test/page", delay_s=0.05):
            pass
    assert time.monotonic() - started >= 0.05
