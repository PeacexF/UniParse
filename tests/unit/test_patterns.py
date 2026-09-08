from __future__ import annotations

import pytest

from uparse.extraction.patterns import detect, strip_price


def _fields(text):
    return [m.field for m in detect(text)]


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("$1,299.00", "price"),
        ("1 299,00 €", "price"),
        ("4.8 out of 5", "rating"),
        ("Sep 8, 2026", "date"),
        ("SKU: AB-12345", "sku"),
        ("In Stock", "availability"),
        ("hello@example.com", "email"),
    ],
)
def test_detects_expected_field(text, field):
    assert _fields(text)[0] == field


def test_prose_matches_nothing():
    assert detect("A quiet paragraph about laptops and their many virtues.") == []


def test_long_text_is_ignored():
    assert detect("$9.99 " * 200) == []


def test_tight_match_scores_higher_than_embedded_one():
    tight = next(m for m in detect("$1,299.00") if m.field == "price")
    loose = next(
        m for m in detect("Now only $1,299.00 for a limited time only") if m.field == "price"
    )
    assert tight.confidence > loose.confidence


def test_strip_price_returns_amount_and_currency():
    assert strip_price("€ 1 299,00") == (1299.0, "EUR")
    assert strip_price("free") == (None, None)
