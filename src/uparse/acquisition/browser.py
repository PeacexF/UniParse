from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any

from uparse.acquisition.base import looks_blocked
from uparse.config.schema import BrowserConfig, Config
from uparse.core.errors import (
    BlockedError,
    BrowserError,
    HttpError,
    NavigationTimeoutError,
    WorkerError,
)
from uparse.core.models import PageModel
from uparse.logging import get_logger
from uparse.runtime.protocol import PROTOCOL_VERSION, Request, Response

log = get_logger("browser")

WORKER_DIR = Path(__file__).resolve().parents[3] / "browser"
WORKER_ENTRY = WORKER_DIR / "dist" / "main.js"
STARTUP_TIMEOUT_S = 30.0


@dataclass(slots=True)
class _Pending:
    """One in-flight request, waiting for the reader thread to hand back its answer."""

    event: threading.Event
    response: Response | None = None
    failure: Exception | None = None


class WorkerProcess:
    """Owns the node child process and the JSONL request/response correlation.

    Requests are correlated by id rather than serialized, so several pages can be in
    flight at once — the worker already pools that many pages, and holding a lock across
    the round trip would have made `browser.concurrency` meaningless.
    """

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self._ids = count(1)
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._proc = self._spawn()
        self._drain = threading.Thread(target=self._pump_stderr, daemon=True)
        self._drain.start()
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()
        self._handshake()

    def _spawn(self) -> subprocess.Popen[str]:
        node = shutil.which("node")
        if node is None:
            raise BrowserError(
                "node is not on PATH; run `make setup` to install the browser worker"
            )
        if not WORKER_ENTRY.exists():
            raise BrowserError(
                f"browser worker not built: {WORKER_ENTRY} is missing (run `make browser`)"
            )
        env = {**os.environ, "UPARSE_PROTOCOL": str(PROTOCOL_VERSION)}
        try:
            return subprocess.Popen(
                [node, str(WORKER_ENTRY)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
                cwd=str(WORKER_DIR),
            )
        except OSError as exc:
            raise BrowserError(f"cannot start browser worker: {exc}") from exc

    def _pump_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        for line in stream:
            text = line.rstrip()
            if text:
                log.debug("worker: %s", text)

    def _handshake(self) -> None:
        data = self.call(
            "hello",
            {
                "protocol": PROTOCOL_VERSION,
                "headless": self.config.headless,
                "timeout": self.config.timeout,
                "concurrency": self.config.concurrency,
                "userAgent": self.config.user_agent,
                "viewport": list(self.config.viewport),
                "locale": self.config.locale,
                "timezone": self.config.timezone,
                "blockResources": self._blocked_types(),
                "extraHeaders": self.config.extra_headers,
                "storageState": str(self.config.storage_state)
                if self.config.storage_state
                else None,
                "executablePath": str(self.config.executable_path)
                if self.config.executable_path
                else None,
                "persistCookies": self.config.persist_cookies,
            },
            timeout=STARTUP_TIMEOUT_S,
        )
        remote = int(data.get("protocol", 0))
        if remote != PROTOCOL_VERSION:
            raise WorkerError(f"worker speaks protocol {remote}, expected {PROTOCOL_VERSION}")

    def _blocked_types(self) -> list[str]:
        blocked = list(self.config.block_resources)
        if self.config.block_images and "image" not in blocked:
            blocked.append("image")
        return blocked

    def call(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        request = Request(id=next(self._ids), method=method, params=params)
        budget = timeout if timeout is not None else self.config.timeout / 1000 + 10
        slot = _Pending(event=threading.Event())
        with self._pending_lock:
            self._pending[request.id] = slot
        try:
            self._write(request)
            if not slot.event.wait(budget):
                raise NavigationTimeoutError(
                    f"worker did not answer {request.method} in {budget:.0f}s"
                )
            if slot.failure is not None:
                raise slot.failure
            response = slot.response
            if response is None:  # pragma: no cover - event set implies one of the two
                raise WorkerError(f"no answer for {request.method}")
            if not response.ok:
                raise _worker_error(response)
            return response.data
        finally:
            with self._pending_lock:
                self._pending.pop(request.id, None)

    def _write(self, request: Request) -> None:
        if self._proc.stdin is None or self._proc.poll() is not None:
            raise WorkerError("browser worker is not running")
        try:
            with self._write_lock:
                self._proc.stdin.write(request.encode() + "\n")
                self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise WorkerError(f"browser worker closed the pipe: {exc}") from exc

    def _pump_stdout(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            return
        for line in stream:
            text = line.strip()
            if not text:
                continue
            try:
                response = Response.decode(text)
            except json.JSONDecodeError:
                log.debug("non-protocol line on stdout: %s", text[:200])
                continue
            with self._pending_lock:
                slot = self._pending.get(response.id)
            if slot is None:
                log.debug("answer for an abandoned request: id=%s", response.id)
                continue
            slot.response = response
            slot.event.set()
        self._fail_pending(WorkerError(f"browser worker exited (code {self._proc.poll()})"))

    def _fail_pending(self, error: Exception) -> None:
        with self._pending_lock:
            waiting = list(self._pending.values())
        for slot in waiting:
            slot.failure = error
            slot.event.set()

    def close(self) -> None:
        if self._proc.poll() is not None:
            return
        with suppress(Exception):
            self.call("shutdown", {}, timeout=5.0)
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired, OSError:
            self._proc.kill()


def _worker_error(response: Response) -> Exception:
    message = response.error or "browser worker error"
    match response.code:
        case "TIMEOUT":
            return NavigationTimeoutError(message)
        case "HTTP":
            return HttpError(message)
        case "NAVIGATION":
            return BrowserError(message)
        case _:
            return WorkerError(message)


class BrowserAcquirer:
    name = "browser"

    def __init__(self, config: Config, worker: WorkerProcess | None = None) -> None:
        self.config = config
        self._worker = worker
        self._owned = worker is None
        self._spawn_lock = threading.Lock()

    @property
    def worker(self) -> WorkerProcess:
        """One worker per acquirer, however many threads ask for it at once.

        Without the lock every thread that raced past the None check spawned its own node
        + Chromium; only the last assignment survived, so the rest were unreachable, never
        closed, and outlived the run burning CPU.
        """
        if self._worker is not None:
            return self._worker
        with self._spawn_lock:
            if self._worker is None:
                self._worker = WorkerProcess(self.config.browser)
            return self._worker

    def fetch(self, url: str, *, depth: int = 0) -> PageModel:
        started = time.perf_counter()
        browser = self.config.browser
        data = self.worker.call(
            "navigate",
            {
                "url": url,
                "waitUntil": browser.wait_until,
                "timeout": browser.timeout,
                "waitFor": browser.wait_for,
                "waitMs": browser.wait_ms,
                "scroll": self.config.pagination.infinite_scroll,
                "maxScrolls": self.config.pagination.max_scrolls,
            },
        )
        html = str(data.get("html") or "")
        status = data.get("status")
        reason = looks_blocked(html, status if isinstance(status, int) else None)
        if reason:
            raise BlockedError(reason, url=url)
        if isinstance(status, int) and status >= 400:
            raise HttpError(f"HTTP {status}", url=url, status=status)
        return PageModel(
            url=url,
            final_url=str(data.get("url") or url),
            status=status if isinstance(status, int) else None,
            html=html,
            title=data.get("title") or None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            acquirer=self.name,
            depth=depth,
            metadata={
                "worker": True,
                "scrolls": data.get("scrolls", 0),
                "pageId": data.get("pageId"),
            },
        )

    def close(self) -> None:
        if self._worker is not None and self._owned:
            self._worker.close()
            self._worker = None
