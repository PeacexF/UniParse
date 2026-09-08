from __future__ import annotations

import pytest

from uparse.core.errors import ErrorCode, StorageError
from uparse.core.models import FieldValue, JobStats, Record, Source, ValueType
from uparse.storage.sqlite import Store


@pytest.fixture
def store():
    with Store() as s:
        s.start_job({"browser": {"concurrency": 1}}, "test")
        yield s


def _record(title="A", price=1.0, index=0):
    rec = Record(index=index, page_url="https://x.test/p", collection="product")
    rec.set(FieldValue(name="title", value=title, confidence=0.9, source=Source.DOM, selector="h2"))
    rec.set(
        FieldValue(
            name="price", value=price, type=ValueType.NUMBER, confidence=0.8, source=Source.PATTERN
        )
    )
    return rec


def test_records_round_trip(store):
    page = store.add_page(store.add_source("https://x.test/p"), "https://x.test/p", status="ok")
    assert store.add_records(page, [_record(), _record("B", 2.0, 1)]) == 2
    assert list(store.iter_records()) == [
        {"title": "A", "price": 1.0},
        {"title": "B", "price": 2.0},
    ]
    assert store.count_records() == 2


def test_provenance_is_stored_separately(store):
    page = store.add_page(store.add_source("https://x.test/p"), "https://x.test/p", status="ok")
    store.add_records(page, [_record()])
    prov = next(iter(store.iter_provenance()))
    assert prov["title"]["source"] == "dom"
    assert prov["title"]["selector"] == "h2"


def test_content_hash_detects_a_repeated_page(store):
    src = store.add_source("https://x.test/p")
    store.add_page(src, "https://x.test/p?page=1", status="ok", content_hash="h1")
    assert store.seen_content_hash(src, "h1") is True
    assert store.seen_content_hash(src, "h2") is False


def test_failed_sources_are_listed_for_retry(store):
    ok = store.add_source("https://x.test/a")
    bad = store.add_source("https://x.test/b")
    store.update_source(ok, "done")
    store.update_source(bad, "failed", error="timeout", attempts=3)
    rows = store.failed_sources()
    assert [r["url"] for r in rows] == ["https://x.test/b"]
    assert rows[0]["attempts"] == 3


def test_errors_are_categorized(store):
    store.add_error(ErrorCode.NAVIGATION_TIMEOUT, "too slow", url="https://x.test/a")
    row = store.conn.execute("SELECT error_type, message FROM errors").fetchone()
    assert row["error_type"] == "NAVIGATION_TIMEOUT"


def test_finish_job_persists_stats(store):
    store.finish_job(JobStats(records=5, pages_processed=2))
    row = store.conn.execute("SELECT status, stats_json FROM jobs").fetchone()
    assert row["status"] == "completed"
    assert '"records": 5' in row["stats_json"]


def test_writing_without_a_job_is_an_error():
    with Store() as s, pytest.raises(StorageError):
        s.add_source("https://x.test/a")


def test_schema_is_created_on_a_real_file(tmp_path):
    path = tmp_path / "nested" / "job.db"
    with Store(path) as s:
        s.start_job({}, "f")
    assert path.exists()
    with Store(path) as s:
        assert s.latest_job_id() == 1
