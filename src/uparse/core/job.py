from __future__ import annotations

import random
import signal
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType

from uparse.acquisition.base import Acquirer, choose_acquirer
from uparse.acquisition.http import FileAcquirer, HttpAcquirer
from uparse.config.schema import Config
from uparse.core.errors import RETRYABLE, BlockedError, ErrorCode, UparseError, code_of
from uparse.core.models import JobStats, PageModel
from uparse.core.pipeline import PageOutcome, process
from uparse.logging import get_logger, progress_line
from uparse.processing.deduplicate import Deduplicator
from uparse.storage.sqlite import Store

log = get_logger("job")


@dataclass(slots=True)
class SourceOutcome:
    url: str
    source_id: int
    pages: int = 0
    records: int = 0
    failed: bool = False
    blocked: bool = False
    error: str | None = None


class Job:
    def __init__(
        self, config: Config, urls: list[str], store: Store, acquirer: Acquirer | None = None
    ) -> None:
        self.config = config
        self.urls = urls
        self.store = store
        self.stats = JobStats(sources=len(urls))
        self.dedupe = Deduplicator(config.dedupe)
        self.acquirer = acquirer or build_acquirer(config, urls)
        self._stop = False

    def run(self) -> JobStats:
        self.store.start_job(self.config.model_dump(mode="json"), self.config.name)
        source_ids = self.store.add_sources(self.urls)
        with _InterruptGuard(self):
            for position, (url, source_id) in enumerate(zip(self.urls, source_ids, strict=True), 1):
                if self._stop:
                    log.warning(
                        "stopping early: %d sources not processed", len(self.urls) - position + 1
                    )
                    break
                outcome = self._run_source(url, source_id)
                progress_line(
                    position,
                    len(self.urls),
                    url,
                    ok=not outcome.failed,
                    detail=_detail(outcome),
                )
                self._pace()
        self.stats.finished_at = _now()
        status = "interrupted" if self._stop else "completed"
        self.store.finish_job(self.stats, status)
        return self.stats

    # ------------------------------------------------------------ sources

    def _run_source(self, url: str, source_id: int) -> SourceOutcome:
        outcome = SourceOutcome(url=url, source_id=source_id)
        seen_urls: set[str] = set()
        if self.config.dedupe.scope == "source":
            self.dedupe.reset()

        current: str | None = url
        depth = 0
        started = time.monotonic()
        while current and depth < self.config.pagination.max_pages:
            if self._stop:
                break
            seen_urls.add(current)
            page, error = self._acquire(current, depth, source_id)
            if page is None:
                if depth == 0:
                    outcome.failed = True
                    outcome.blocked = isinstance(error, BlockedError)
                    outcome.error = str(error)
                    self.store.update_source(
                        source_id, "blocked" if outcome.blocked else "failed", error=str(error)
                    )
                    return outcome
                break

            if self.config.pagination.stop_on_duplicate_page and self.store.seen_content_hash(
                source_id, page.content_hash()
            ):
                log.debug("duplicate page content, stopping pagination: %s", current)
                break

            result = self._process_page(page, source_id, seen_urls)
            outcome.pages += 1
            outcome.records += result[0]
            self.stats.pages_processed += 1

            if outcome.records >= self.config.pagination.max_items:
                break
            if time.monotonic() - started > self.config.pagination.max_duration_s:
                log.debug("pagination time budget reached for %s", url)
                break
            current = result[1]
            depth += 1
            if current:
                self._pace()

        self.store.update_source(source_id, "done")
        return outcome

    def _process_page(
        self, page: PageModel, source_id: int, seen_urls: set[str]
    ) -> tuple[int, str | None]:
        page_id = self.store.add_page(
            source_id,
            page.url,
            status="ok",
            final_url=page.final_url,
            http_status=page.status,
            title=page.title,
            depth=page.depth,
            content_hash=page.content_hash(),
            elapsed_ms=page.elapsed_ms,
        )
        try:
            outcome: PageOutcome = process(page, self.config, seen_urls=seen_urls)
        except Exception as exc:  # extraction must never kill a job
            self._record_error(exc, source_id=source_id, page_id=page_id, url=page.url)
            return 0, None

        fresh = [r for r in outcome.records if not self.dedupe.is_duplicate(r)]
        self.stats.duplicates = self.dedupe.duplicates
        if fresh:
            self.store.add_records(page_id, fresh, keys=self.config.dedupe.keys or None)
            self.stats.records += len(fresh)
        if outcome.report and outcome.report.dropped:
            log.debug("%d records dropped by validation on %s", outcome.report.dropped, page.url)

        next_url = outcome.next_hint.url if outcome.next_hint else None
        if next_url:
            seen_urls.add(next_url)
        return len(fresh), next_url

    # ---------------------------------------------------------- acquiring

    def _acquire(
        self, url: str, depth: int, source_id: int
    ) -> tuple[PageModel | None, Exception | None]:
        retry = self.config.retry
        last: Exception | None = None
        for attempt in range(1, retry.attempts + 1):
            try:
                return self.acquirer.fetch(url, depth=depth), None
            except Exception as exc:
                last = exc
                code = code_of(exc)
                if code not in RETRYABLE or attempt == retry.attempts:
                    break
                self.stats.retries += 1
                delay = _backoff(attempt, retry.base_delay_s, retry.max_delay_s, retry.jitter)
                log.debug(
                    "retry %d/%d in %.1fs after %s: %s", attempt, retry.attempts, delay, code, exc
                )
                time.sleep(delay)
        assert last is not None
        self._record_error(last, source_id=source_id, url=url)
        return None, last

    def _record_error(
        self, exc: Exception, *, source_id: int, page_id: int | None = None, url: str | None = None
    ) -> None:
        code = code_of(exc)
        message = exc.message if isinstance(exc, UparseError) else f"{type(exc).__name__}: {exc}"
        self.stats.record_error(str(code))
        if code is ErrorCode.BLOCKED:
            self.stats.pages_blocked += 1
        else:
            self.stats.pages_failed += 1
        self.store.add_error(code, message, source_id=source_id, page_id=page_id, url=url)

    def _pace(self) -> None:
        if self.config.politeness.delay_s > 0:
            time.sleep(self.config.politeness.delay_s)

    def stop(self) -> None:
        self._stop = True

    def iter_records(self) -> Iterator[dict[str, object]]:
        return self.store.iter_records()

    def close(self) -> None:
        self.acquirer.close()


def build_acquirer(config: Config, urls: list[str]) -> Acquirer:
    match choose_acquirer(config, urls):
        case "file":
            return FileAcquirer(config)
        case "browser":
            from uparse.acquisition.browser import BrowserAcquirer

            return BrowserAcquirer(config)
        case _:
            return HttpAcquirer(config)


def _backoff(attempt: int, base: float, cap: float, jitter: float) -> float:
    delay = min(base * float(2 ** (attempt - 1)), cap)
    return delay * (1.0 + random.uniform(-jitter, jitter)) if jitter else delay


def _detail(outcome: SourceOutcome) -> str:
    if outcome.blocked:
        return "[warn]BLOCKED — manual intervention required[/warn]"
    if outcome.failed:
        return f"[fail]{outcome.error}[/fail]"
    pages = f" over {outcome.pages} pages" if outcome.pages > 1 else ""
    return f"{outcome.records} records{pages}"


def _now() -> datetime:
    return datetime.now(UTC)


class _InterruptGuard:
    # Ctrl-C asks the job to stop cleanly; a second one is left to Python.

    def __init__(self, job: Job) -> None:
        self.job = job
        self.previous: Handler = None

    def __enter__(self) -> None:
        def handler(signum: int, frame: FrameType | None) -> None:
            del signum, frame
            log.warning("interrupt received, finishing the current page and flushing")
            self.job.stop()
            signal.signal(signal.SIGINT, signal.default_int_handler)

        try:
            self.previous = signal.signal(signal.SIGINT, handler)
        except ValueError:  # not on the main thread
            self.previous = None

    def __exit__(self, *exc: object) -> None:
        if self.previous is not None:
            signal.signal(signal.SIGINT, self.previous)


Handler = Callable[[int, FrameType | None], object] | int | None
