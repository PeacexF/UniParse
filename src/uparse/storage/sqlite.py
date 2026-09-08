from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Concatenate, Self

from uparse.core.errors import ErrorCode, StorageError
from uparse.core.models import JobStats, Record

SCHEMA_VERSION = "1"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def locked[**P, R](
    method: Callable[Concatenate[Store, P], R],
) -> Callable[Concatenate[Store, P], R]:
    """Serialize one statement (or one transaction) against the shared connection."""

    def guarded(self: Store, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self.lock:
            return method(self, *args, **kwargs)

    # Not functools.wraps: its return type would no longer match the signature above,
    # and `cast` cannot help because annotations here are strings but cast is evaluated.
    guarded.__name__ = method.__name__
    guarded.__qualname__ = getattr(method, "__qualname__", method.__name__)
    guarded.__doc__ = method.__doc__
    return guarded


class Store:
    """Job persistence. Everything the pipeline writes goes through here.

    One connection is shared across worker threads, so every statement runs under
    `self.lock`: SQLite tolerates the sharing, but interleaved BEGIN/COMMIT from two
    threads would not be two transactions, it would be one confused one.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.lock = threading.RLock()
        self.path = Path(path) if path is not None else None
        target = ":memory:" if self.path is None else str(self.path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as exc:
            raise StorageError(f"cannot open database {target}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        self.job_id: int | None = None

    def _configure(self) -> None:
        cur = self.conn.cursor()
        if self.path is not None:
            cur.execute("PRAGMA journal_mode = WAL")
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA busy_timeout = 5000")

    def _migrate(self) -> None:
        # executescript implicitly commits, so it must not run inside transaction().
        sql = resources.files("uparse.storage").joinpath("schema.sql").read_text(encoding="utf-8")
        self.conn.executescript(sql)
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN")
            try:
                yield self.conn
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            cur.execute("COMMIT")

    # -------------------------------------------------------------- jobs

    @locked
    def start_job(self, config: dict[str, Any], name: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO jobs(name, created_at, status, config_json) VALUES(?,?,?,?)",
            (name, _now(), "running", _dumps(config)),
        )
        self.job_id = int(cur.lastrowid or 0)
        return self.job_id

    @locked
    def finish_job(self, stats: JobStats, status: str = "completed") -> None:
        self.conn.execute(
            "UPDATE jobs SET completed_at=?, status=?, stats_json=? WHERE id=?",
            (_now(), status, _dumps(stats.as_dict()), self._job()),
        )

    def _job(self) -> int:
        if self.job_id is None:
            raise StorageError("no active job; call start_job() first")
        return self.job_id

    # ----------------------------------------------------------- sources

    @locked
    def add_source(self, url: str, status: str = "pending") -> int:
        cur = self.conn.execute(
            "INSERT INTO sources(job_id, url, status) VALUES(?,?,?)", (self._job(), url, status)
        )
        return int(cur.lastrowid or 0)

    @locked
    def add_sources(self, urls: Iterable[str]) -> list[int]:
        with self.transaction():
            return [self.add_source(u) for u in urls]

    @locked
    def update_source(
        self, source_id: int, status: str, *, error: str | None = None, attempts: int | None = None
    ) -> None:
        if attempts is None:
            self.conn.execute(
                "UPDATE sources SET status=?, error=? WHERE id=?", (status, error, source_id)
            )
        else:
            self.conn.execute(
                "UPDATE sources SET status=?, error=?, attempts=? WHERE id=?",
                (status, error, attempts, source_id),
            )

    @locked
    def failed_sources(self, job_id: int | None = None) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM sources WHERE job_id=? AND status IN ('failed','blocked') ORDER BY id",
                (job_id if job_id is not None else self._job(),),
            )
        )

    @locked
    def job_config(self, job_id: int) -> dict[str, Any]:
        row = self.conn.execute("SELECT config_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return {}
        loaded = json.loads(row["config_json"])
        return loaded if isinstance(loaded, dict) else {}

    @locked
    def latest_job_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else int(row["id"])

    @locked
    def mark_retried(self, job_id: int) -> None:
        """Hand failures over to the retry job so `retry` is not an infinite loop."""
        self.conn.execute(
            "UPDATE sources SET status='retried' WHERE job_id=? AND status IN ('failed','blocked')",
            (job_id,),
        )

    @locked
    def latest_job_with_failures(self) -> int | None:
        """Retrying twice must not target the previous retry run, which has no failures."""
        row = self.conn.execute(
            "SELECT job_id FROM sources WHERE status IN ('failed','blocked')"
            " ORDER BY job_id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else int(row["job_id"])

    # ------------------------------------------------------------- pages

    @locked
    def add_page(
        self,
        source_id: int,
        url: str,
        *,
        status: str,
        final_url: str | None = None,
        http_status: int | None = None,
        title: str | None = None,
        depth: int = 0,
        content_hash: str | None = None,
        elapsed_ms: int | None = None,
        error: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO pages(source_id, url, final_url, status, http_status, title, depth,"
            " content_hash, loaded_at, elapsed_ms, error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                url,
                final_url,
                status,
                http_status,
                title,
                depth,
                content_hash,
                _now(),
                elapsed_ms,
                error,
            ),
        )
        return int(cur.lastrowid or 0)

    @locked
    def seen_content_hash(self, source_id: int, content_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM pages WHERE source_id=? AND content_hash=? LIMIT 1",
            (source_id, content_hash),
        ).fetchone()
        return row is not None

    # ----------------------------------------------------------- records

    @locked
    def add_records(
        self, page_id: int, records: Iterable[Record], *, keys: list[str] | None = None
    ) -> int:
        count = 0
        with self.transaction():
            for rec in records:
                cur = self.conn.execute(
                    "INSERT INTO records(page_id, record_index, collection, identity, confidence, data_json)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        page_id,
                        rec.index,
                        rec.collection or None,
                        rec.identity(keys) if keys else rec.identity(),
                        round(rec.confidence, 4),
                        _dumps(rec.to_dict()),
                    ),
                )
                record_id = int(cur.lastrowid or 0)
                self.conn.executemany(
                    "INSERT INTO fields(record_id, name, value_json, type, confidence, source,"
                    " selector, provenance_json) VALUES(?,?,?,?,?,?,?,?)",
                    [
                        (
                            record_id,
                            fv.name,
                            _dumps(fv.value),
                            str(fv.type),
                            round(fv.confidence, 4),
                            str(fv.source),
                            fv.selector,
                            _dumps(fv.provenance()),
                        )
                        for fv in rec.fields.values()
                    ],
                )
                count += 1
        return count

    def iter_records(self, job_id: int | None = None, batch: int = 500) -> Iterator[dict[str, Any]]:
        jid = job_id if job_id is not None else self._job()
        cur = self.conn.execute(
            "SELECT r.data_json FROM records r"
            " JOIN pages p ON p.id = r.page_id"
            " JOIN sources s ON s.id = p.source_id"
            " WHERE s.job_id = ? ORDER BY s.id, p.id, r.record_index, r.id",
            (jid,),
        )
        while rows := cur.fetchmany(batch):
            for row in rows:
                yield json.loads(row["data_json"])

    def iter_provenance(self, job_id: int | None = None) -> Iterator[dict[str, Any]]:
        jid = job_id if job_id is not None else self._job()
        cur = self.conn.execute(
            "SELECT r.id AS rid, f.name, f.provenance_json FROM records r"
            " JOIN pages p ON p.id = r.page_id"
            " JOIN sources s ON s.id = p.source_id"
            " LEFT JOIN fields f ON f.record_id = r.id"
            " WHERE s.job_id = ? ORDER BY s.id, p.id, r.record_index, r.id, f.id",
            (jid,),
        )
        current_id: int | None = None
        current: dict[str, Any] = {}
        for row in cur:
            if row["rid"] != current_id:
                if current_id is not None:
                    yield current
                current_id, current = int(row["rid"]), {}
            if row["name"] is not None:
                current[row["name"]] = json.loads(row["provenance_json"])
        if current_id is not None:
            yield current

    @locked
    def count_records(self, job_id: int | None = None) -> int:
        jid = job_id if job_id is not None else self._job()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM records r JOIN pages p ON p.id=r.page_id"
            " JOIN sources s ON s.id=p.source_id WHERE s.job_id=?",
            (jid,),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ errors

    @locked
    def add_error(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        source_id: int | None = None,
        page_id: int | None = None,
        url: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO errors(job_id, source_id, page_id, error_type, message, url, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (self._job(), source_id, page_id, str(code), message[:4000], url, _now()),
        )

    # ------------------------------------------------------------- misc

    @locked
    def close(self) -> None:
        with suppress(sqlite3.Error):
            self.conn.commit()
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
