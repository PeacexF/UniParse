from __future__ import annotations

import pytest

from uparse.core.models import ValueType
from uparse.processing.normalize import (
    coerce,
    normalize_enum,
    normalize_text,
    normalize_url,
    parse_boolean,
    parse_currency,
    parse_date,
    parse_number,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,299.00", 1299.0),
        ("1 299,00 €", 1299.0),
        ("1.299,00", 1299.0),
        ("12.99", 12.99),
        ("12,99", 12.99),
        ("1,299", 1299.0),
        ("1299", 1299.0),
        ("-15.5%", -15.5),
        ("4.8/5", 4.8),
        ("no digits", None),
    ],
)
def test_parse_number_is_locale_tolerant(raw, expected):
    assert parse_number(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [("$9.99", "USD"), ("1 299,00 €", "EUR"), ("£10", "GBP"), ("10 USD", "USD"), ("10", None)],
)
def test_parse_currency(raw, code):
    assert parse_currency(raw) == code


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-08", "2026-09-08T00:00:00"),
        ("Sep 8, 2026", "2026-09-08T00:00:00"),
        ("2026-09-08T10:00:00Z", "2026-09-08T10:00:00+00:00"),
        ("yesterday", None),
        ("12", None),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("yes", True), ("In Stock", True), ("Out of Stock", False), ("no", False), ("maybe", None)],
)
def test_parse_boolean(raw, expected):
    assert parse_boolean(raw) is expected


def test_normalize_text_collapses_unicode_spaces():
    assert normalize_text("  a  b\n\tc  ") == "a b c"


def test_normalize_url_resolves_relative():
    assert normalize_url("../p/1", "https://x.test/a/b/") == "https://x.test/a/p/1"


def test_normalize_url_rejects_javascript():
    assert normalize_url("javascript:void(0)") == ""


def test_normalize_enum_unwraps_schema_org():
    assert normalize_enum("https://schema.org/InStock") == "in stock"
    assert normalize_enum("plain") == "plain"


def test_coerce_preserves_original_when_conversion_fails():
    value, vtype = coerce("call for price", ValueType.NUMBER)
    assert value == "call for price"
    assert vtype is ValueType.STRING


def test_coerce_converts_when_confident():
    assert coerce("$1,299.00", ValueType.NUMBER) == (1299.0, ValueType.NUMBER)
