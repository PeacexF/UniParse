from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from uparse.acquisition.base import looks_blocked
from uparse.config.schema import Config
from uparse.core.errors import BlockedError, HttpError, NavigationTimeoutError
from uparse.core.models import PageModel

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 uparse/0.1"


class HttpAcquirer:
    """No JavaScript. Fast path for static pages and the backbone of the test suite."""

    name = "http"

    def __init__(self, config: Config) -> None:
        self.config = config
        headers: dict[str, str] = {
            "User-Agent": config.browser.user_agent or DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": config.browser.locale or "en-US,en;q=0.9",
            **config.browser.extra_headers,
        }
        self.client = httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=config.browser.timeout / 1000,
            http2=False,
        )

    def fetch(self, url: str, *, depth: int = 0) -> PageModel:
        started = time.perf_counter()
        try:
            response = self.client.get(url)
        except httpx.TimeoutException as exc:
            raise NavigationTimeoutError(str(exc), url=url) from exc
        except httpx.HTTPError as exc:
            raise HttpError(str(exc), url=url) from exc

        html = response.text if _is_html(response) else ""
        reason = looks_blocked(html, response.status_code)
        if reason:
            raise BlockedError(reason, url=url)
        if response.status_code >= 400:
            raise HttpError(f"HTTP {response.status_code}", url=url, status=response.status_code)
        return PageModel(
            url=url,
            final_url=str(response.url),
            status=response.status_code,
            html=html,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            acquirer=self.name,
            depth=depth,
            metadata=_meta(response),
        )

    def close(self) -> None:
        self.client.close()


def _is_html(response: httpx.Response) -> bool:
    ctype = response.headers.get("content-type", "")
    return "html" in ctype or "xml" in ctype or not ctype


def _meta(response: httpx.Response) -> dict[str, Any]:
    return {
        "content_type": response.headers.get("content-type", ""),
        "redirects": [str(r.url) for r in response.history],
    }


class FileAcquirer:
    """Reads file:// URLs and local paths. Used by fixtures and saved-HTML jobs."""

    name = "file"

    def __init__(self, config: Config) -> None:
        self.config = config

    def fetch(self, url: str, *, depth: int = 0) -> PageModel:
        started = time.perf_counter()
        path = Path(url.removeprefix("file://")) if url.startswith("file://") else Path(url)
        try:
            html = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HttpError(f"cannot read {path}: {exc}", url=url) from exc
        return PageModel(
            url=url,
            final_url=path.resolve().as_uri(),
            status=200,
            html=html,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            acquirer=self.name,
            depth=depth,
        )

    def close(self) -> None:
        return
