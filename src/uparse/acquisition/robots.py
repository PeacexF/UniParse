"""robots.txt, honoured rather than merely configured.

`politeness.respect_robots` defaulted to true and did nothing. A universal parser that
ignores the one machine-readable statement of a site's wishes is not being universal,
it is being rude, so the flag now means what it says. Fetch failures are permissive:
an unreachable robots.txt is not a prohibition.
"""

from __future__ import annotations

import threading
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from uparse.logging import get_logger

log = get_logger("robots")

FETCH_TIMEOUT_S = 10.0


class RobotsPolicy:
    """One parsed robots.txt per host, fetched at most once per job."""

    def __init__(
        self, user_agent: str, *, enabled: bool = True, timeout_s: float = FETCH_TIMEOUT_S
    ):
        self.user_agent = user_agent
        self.enabled = enabled
        self.timeout_s = timeout_s
        self._rules: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allows(self, url: str) -> bool:
        if not self.enabled:
            return True
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return True
        rules = self._for(f"{parsed.scheme}://{parsed.netloc}")
        if rules is None:
            return True
        return bool(rules.can_fetch(self.user_agent, url))

    def crawl_delay(self, url: str) -> float:
        if not self.enabled:
            return 0.0
        parsed = urlsplit(url)
        rules = self._for(f"{parsed.scheme}://{parsed.netloc}")
        if rules is None:
            return 0.0
        try:
            declared = rules.crawl_delay(self.user_agent)
        except Exception:  # a malformed directive must not stop the job
            return 0.0
        return float(declared) if declared else 0.0

    def _for(self, origin: str) -> RobotFileParser | None:
        with self._lock:
            if origin in self._rules:
                return self._rules[origin]
            self._rules[origin] = None  # claim the slot; a second thread waits on the lock
            rules = self._fetch(origin)
            self._rules[origin] = rules
            return rules

    def _fetch(self, origin: str) -> RobotFileParser | None:
        parsed = urlsplit(origin)
        target = urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        try:
            response = httpx.get(
                target,
                timeout=self.timeout_s,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        except httpx.HTTPError as exc:
            log.debug("no robots.txt for %s (%s); allowing", origin, exc)
            return None
        if response.status_code >= 400:
            log.debug("robots.txt for %s returned %d; allowing", origin, response.status_code)
            return None
        rules = RobotFileParser()
        rules.parse(response.text.splitlines())
        log.debug("robots.txt loaded for %s", origin)
        return rules
