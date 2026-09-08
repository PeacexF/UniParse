from __future__ import annotations

from uparse.config.schema import DedupeConfig, ExtractionConfig
from uparse.core.models import FieldValue, Record, Source, ValueType
from uparse.processing.deduplicate import Deduplicator
from uparse.processing.validation import validate


def _record(index=0, **values):
    rec = Record(index=index)
    for name, value in values.items():
        vtype = ValueType.URL if name in {"url", "image"} else ValueType.STRING
        rec.set(FieldValue(name=name, value=value, type=vtype, confidence=0.8, source=Source.DOM))
    return rec


def test_canonical_url_is_the_strongest_identity():
    dedupe = Deduplicator(DedupeConfig())
    assert not dedupe.is_duplicate(_record(url="https://x.test/1", title="A"))
    assert dedupe.is_duplicate(_record(url="https://x.test/1", title="A different title"))


def test_identical_titles_alone_are_not_duplicates():
    dedupe = Deduplicator(DedupeConfig())
    assert not dedupe.is_duplicate(_record(title="Same", url="https://x.test/1"))
    assert not dedupe.is_duplicate(_record(title="Same", url="https://x.test/2"))


def test_configured_keys_win_over_url():
    dedupe = Deduplicator(DedupeConfig(keys=["sku"]))
    assert not dedupe.is_duplicate(_record(sku="A1", url="https://x.test/1"))
    assert dedupe.is_duplicate(_record(sku="A1", url="https://x.test/2"))


def test_records_without_identity_fall_back_to_a_content_hash():
    dedupe = Deduplicator(DedupeConfig())
    assert not dedupe.is_duplicate(_record(title="A", price="1"))
    assert dedupe.is_duplicate(_record(title="A", price="1"))
    assert not dedupe.is_duplicate(_record(title="A", price="2"))


def test_dedupe_can_be_disabled():
    dedupe = Deduplicator(DedupeConfig(enabled=False))
    record = _record(url="https://x.test/1")
    assert not dedupe.is_duplicate(record)
    assert not dedupe.is_duplicate(record)


def test_empty_records_are_dropped():
    report = validate([_record(), _record(title="A")], ExtractionConfig())
    assert report.dropped == 1
    assert len(report.kept) == 1


def test_missing_required_field_drops_the_record():
    config = ExtractionConfig(
        fields={"title": "auto", "price": {"selector": ".p", "required": True}}
    )
    report = validate([_record(title="A"), _record(title="B", price="9")], config)
    assert [r.get("title") for r in report.kept] == ["B"]
    assert any(p.reason == "required field missing" for p in report.problems)


def test_relative_urls_are_reported_but_not_fatal():
    report = validate([_record(title="A", url="/relative")], ExtractionConfig())
    assert len(report.kept) == 1
    assert any(p.reason == "URL is not absolute" for p in report.problems)
