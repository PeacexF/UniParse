from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from uparse.config.schema import Config
from uparse.core.models import PageModel

# Challenge pages we detect and report. UniParse never tries to pass them.
_BLOCK_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"\bcf-browser-verification\b",
        r"\bcf-challenge\b",
        r"Checking your browser before accessing",
        r"Just a moment\.\.\.",
        r"\bg-recaptcha\b",
        r"\bh-captcha\b",
        r"data-sitekey=",
        r"Access denied.{0,60}(Cloudflare|Akamai|Incapsula)",
        r"Request unsuccessful\. Incapsula",
        r"\bpx-captcha\b",
        r"Please verify you are a human",
        r"unusual traffic from your computer network",
    )
)

BLOCK_STATUS = frozenset({401, 403, 429, 503})


def looks_blocked(html: str, status: int | None) -> str | None:
    """Return a human-readable reason when the page looks like an access challenge."""
    head = html[:20_000]
    for pattern in _BLOCK_MARKERS:
        if pattern.search(head):
            return f"challenge marker: {pattern.pattern}"
    if status in BLOCK_STATUS and len(html) < 4_000:
        return f"HTTP {status} with a short body"
    return None


@runtime_checkable
class Acquirer(Protocol):
    name: str

    def fetch(self, url: str, *, depth: int = 0) -> PageModel: ...

    def close(self) -> None: ...


def choose_acquirer(config: Config, urls: list[str]) -> str:
    if config.acquirer != "auto":
        return config.acquirer
    if urls and all(u.startswith("file://") for u in urls):
        return "file"
    return "browser" if config.browser.enabled else "http"
