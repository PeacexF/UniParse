from __future__ import annotations

import random
import signal
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType

from uparse.acquisition.base import Acquirer, choose_acquirer
from uparse.acquisition.http import DEFAULT_UA, FileAcquirer, HttpAcquirer
from uparse.acquisition.robots import RobotsPolicy
from uparse.config.schema import Config
from uparse.core.errors import (
    RETRYABLE,
    BlockedError,
    ErrorCode,
    RobotsDisallowedError,
    UparseError,
    code_of,
)
from uparse.core.limits import HostLimiter, host_of
from uparse.core.models import JobStats, PageModel, Record
from uparse.core.pipeline import PageOutcome, process
from uparse.extraction.engine import extract_one
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
    followed: int = 0
    failed: bool = False
    blocked: bool = False
    error: str | None = None


class Job:
    """Runs the sources: acquire, extract, follow, store.

    Sources run in parallel up to `config.concurrency`; pagination inside one source stays
    sequential because each next page is only discoverable from the current one. Politeness
    is enforced per host rather than per job, so parallelism across many sites never turns
    into a burst against one of them.
    """

    def __init__(
        self, config: Config, urls: list[str], store: Store, acquirer: Acquirer | None = None
    ) -> None:
        self.config = config
        self.urls = urls
        self.store = store
        self.stats = JobStats(sources=len(urls))
        self.dedupe = Deduplicator(config.dedupe)
        self.acquirer = acquirer or build_acquirer(config, urls)
        self.limiter = HostLimiter(
            per_host=config.politeness.per_host, delay_s=config.politeness.delay_s
        )
        self.robots = RobotsPolicy(
            config.browser.user_agent or DEFAULT_UA,
            enabled=config.politeness.respect_robots,
        )
        self._stop = False
        self._stats_lock = threading.Lock()
        self._visited: set[str] = set()
        self._visited_lock = threading.Lock()
        self._follow_left = config.follow.max_pages if config.follow.enabled else 0
        self._details: ThreadPoolExecutor | None = None

    @property
    def workers(self) -> int:
        return max(1, min(self.config.concurrency, len(self.urls)))

    def run(self) -> JobStats:
        self.store.start_job(self.config.model_dump(mode="json"), self.config.name)
        source_ids = self.store.add_sources(self.urls)
        pairs = list(zip(self.urls, source_ids, strict=True))
        with _InterruptGuard(self), ExitStack() as stack:
            if self.config.follow.enabled:
                # A separate pool: source threads wait on detail fetches, and a detail
                # fetch never queues more work, so the two pools cannot deadlock together.
                self._details = stack.enter_context(
                    ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="uparse-detail")
                )
            if self.workers == 1:
                self._run_serial(pairs)
            else:
                self._run_parallel(pairs, stack)
        self.stats.finished_at = _now()
        self.store.finish_job(self.stats, "interrupted" if self._stop else "completed")
        return self.stats

    def _run_serial(self, pairs: list[tuple[str, int]]) -> None:
        for position, (url, source_id) in enumerate(pairs, 1):
            if self._stop:
                log.warning("stopping early: %d sources not processed", len(pairs) - position + 1)
                break
            self._report(position, len(pairs), self._run_source(url, source_id))

    def _run_parallel(self, pairs: list[tuple[str, int]], stack: ExitStack) -> None:
        pool = stack.enter_context(
            ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="uparse-source")
        )
        log.debug("processing %d sources with %d workers", len(pairs), self.workers)
        futures: dict[Future[SourceOutcome], str] = {
            pool.submit(self._run_source, url, source_id): url for url, source_id in pairs
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                outcome = future.result()
            except Exception as exc:  # a crashed worker must not lose the rest of the job
                log.debug("source task failed: %s", exc)
                continue
            self._report(done, len(pairs), outcome)

    def _report(self, position: int, total: int, outcome: SourceOutcome) -> None:
        progress_line(position, total, outcome.url, ok=not outcome.failed, detail=_detail(outcome))

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
                    outcome.blocked = isinstance(error, BlockedError | RobotsDisallowedError)
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

            written, followed, next_url = self._process_page(page, source_id, seen_urls)
            outcome.pages += 1
            outcome.records += written
            outcome.followed += followed
            with self._stats_lock:
                self.stats.pages_processed += 1

            if outcome.records >= self.config.pagination.max_items:
                break
            if time.monotonic() - started > self.config.pagination.max_duration_s:
                log.debug("pagination time budget reached for %s", url)
                break
            current = next_url
            depth += 1

        self.store.update_source(source_id, "done")
        return outcome

    def _process_page(
        self, page: PageModel, source_id: int, seen_urls: set[str]
    ) -> tuple[int, int, str | None]:
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
            return 0, 0, None

        followed = self._follow(outcome.records, source_id) if self.config.follow.enabled else 0
        fresh = [r for r in outcome.records if not self.dedupe.is_duplicate(r)]
        if fresh:
            self.store.add_records(page_id, fresh, keys=self.config.dedupe.keys or None)
        with self._stats_lock:
            self.stats.duplicates = self.dedupe.duplicates
            self.stats.records += len(fresh)
        if outcome.report and outcome.report.dropped:
            log.debug("%d records dropped by validation on %s", outcome.report.dropped, page.url)

        next_url = outcome.next_hint.url if outcome.next_hint else None
        if next_url:
            seen_urls.add(next_url)
        return len(fresh), followed, next_url

    # ------------------------------------------------------------- follow

    def _follow(self, records: list[Record], source_id: int) -> int:
        """Fetch the page each record points at and merge its fields back into the record."""
        pool = self._details
        if pool is None:
            return 0
        targets = [(record, self._detail_url(record)) for record in records]
        pending = {
            pool.submit(self._detail, url, source_id): record
            for record, url in targets
            if url is not None
        }
        merged = 0
        for future in as_completed(pending):
            detail = future.result()
            if detail is None:
                continue
            _merge(pending[future], detail, prefer_detail=self.config.follow.prefer == "detail")
            merged += 1
        with self._stats_lock:
            self.stats.followed += merged
        return merged

    def _detail_url(self, record: Record) -> str | None:
        value = record.get(self.config.follow.field)
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            return None
        if self.config.follow.same_host and host_of(value) != host_of(record.page_url):
            return None
        with self._visited_lock:
            if value in self._visited or self._follow_left <= 0:
                return None
            self._visited.add(value)
            self._follow_left -= 1
        return value

    def _detail(self, url: str, source_id: int) -> Record | None:
        page, _ = self._acquire(url, depth=0, source_id=source_id, quiet=True)
        if page is None:
            return None
        try:
            record = extract_one(page, self.config.extraction)
        except Exception as exc:
            log.debug("detail extraction failed for %s: %s", url, exc)
            return None
        with self._stats_lock:
            self.stats.pages_processed += 1
        return record

    # ---------------------------------------------------------- acquiring

    def _acquire(
        self, url: str, depth: int, source_id: int, *, quiet: bool = False
    ) -> tuple[PageModel | None, Exception | None]:
        retry = self.config.retry
        last: Exception | None = None
        for attempt in range(1, retry.attempts + 1):
            try:
                return self._fetch(url, depth), None
            except Exception as exc:
                last = exc
                code = code_of(exc)
                if code not in RETRYABLE or attempt == retry.attempts:
                    break
                with self._stats_lock:
                    self.stats.retries += 1
                delay = _backoff(attempt, retry.base_delay_s, retry.max_delay_s, retry.jitter)
                log.debug(
                    "retry %d/%d in %.1fs after %s: %s", attempt, retry.attempts, delay, code, exc
                )
                time.sleep(delay)
        assert last is not None
        if not quiet or code_of(last) is not ErrorCode.ROBOTS_DISALLOWED:
            self._record_error(last, source_id=source_id, url=url)
        return None, last

    def _fetch(self, url: str, depth: int) -> PageModel:
        if not self.robots.allows(url):
            raise RobotsDisallowedError("disallowed by the site's robots.txt", url=url)
        crawl_delay = (
            self.robots.crawl_delay(url) if self.config.politeness.obey_crawl_delay else 0.0
        )
        with self.limiter.hold(url, delay_s=crawl_delay):
            return self.acquirer.fetch(url, depth=depth)

    def _record_error(
        self, exc: Exception, *, source_id: int, page_id: int | None = None, url: str | None = None
    ) -> None:
        code = code_of(exc)
        message = exc.message if isinstance(exc, UparseError) else f"{type(exc).__name__}: {exc}"
        with self._stats_lock:
            self.stats.record_error(str(code))
            if code in (ErrorCode.BLOCKED, ErrorCode.ROBOTS_DISALLOWED):
                self.stats.pages_blocked += 1
            else:
                self.stats.pages_failed += 1
        self.store.add_error(code, message, source_id=source_id, page_id=page_id, url=url)

    def stop(self) -> None:
        self._stop = True

    def iter_records(self) -> Iterator[dict[str, object]]:
        return self.store.iter_records()

    def close(self) -> None:
        self.acquirer.close()


# The listing's link is the record's identity and how it was reached; a detail page
# restating its own canonical URL must not renumber the row it was fetched for.
_KEEP_FROM_LISTING = frozenset({"url"})


def _merge(record: Record, detail: Record, *, prefer_detail: bool) -> None:
    """Fold a detail page's fields into the listing record without losing what it had."""
    for name, value in detail.fields.items():
        if value.value in (None, "") or name in _KEEP_FROM_LISTING:
            continue
        existing = record.fields.get(name)
        if existing is not None and _is_chrome(name, existing.value, value.value):
            continue
        if existing is None or (prefer_detail and existing.value != value.value):
            record.set(value)


def _is_chrome(name: str, listing: object, detail: object) -> bool:
    """A detail <title> is usually the listing's title plus the site name. Keep the short one."""
    if name != "title" or not isinstance(listing, str) or not isinstance(detail, str):
        return False
    return len(detail) > len(listing) and listing.strip() in detail


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
    followed = f", {outcome.followed} followed" if outcome.followed else ""
    return f"{outcome.records} records{pages}{followed}"


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
            with suppress(ValueError):
                signal.signal(signal.SIGINT, self.previous)


Handler = Callable[[int, FrameType | None], object] | int | None
