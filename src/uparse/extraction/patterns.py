from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from uparse.processing.normalize import CURRENCY_CODES, CURRENCY_SYMBOLS, parse_date, parse_number

_SYMBOLS = "".join(re.escape(s) for s in CURRENCY_SYMBOLS if len(s) == 1)
_CODES = "|".join(sorted(CURRENCY_CODES))
_NUMBER = r"\d{1,3}(?:[.,  ]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"

PRICE = re.compile(
    rf"(?:[{_SYMBOLS}]|\b(?:{_CODES})\b)\s?({_NUMBER})|({_NUMBER})\s?(?:[{_SYMBOLS}]|\b(?:{_CODES})\b)"
)
RATING = re.compile(
    r"\b([0-5](?:[.,]\d)?)\s*(?:/|out of|of)\s*5\b|\b([0-5](?:[.,]\d)?)\s*(?:stars?|★)", re.I
)
STARS = re.compile(r"[★☆]{3,}")
PERCENT = re.compile(rf"({_NUMBER})\s?%")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]?\d{2,4}){1,3}")
SKU = re.compile(
    r"\b(?:SKU|MPN|EAN|UPC|ISBN(?:-1[03])?|Art\.?\s?(?:No|Nr)\.?|Model)\b[:\s#]*([\w-]{4,})", re.I
)
DATE_TEXT = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b",
    re.I,
)
RELATIVE_TIME = re.compile(r"\b\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago\b", re.I)
AVAILABILITY = re.compile(
    r"\b(in stock|out of stock|sold out|available|unavailable|pre-?order|backorder)\b", re.I
)
DURATION = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b")


@dataclass(slots=True, frozen=True)
class PatternHit:
    field: str
    value: object
    confidence: float
    detail: str


def detect(text: str) -> list[PatternHit]:
    """Field guesses for a short piece of text, strongest first."""
    if not text or len(text) > 400:
        return []
    return sorted(_detect(text), key=lambda m: -m.confidence)


def _detect(text: str) -> Iterator[PatternHit]:
    stripped = text.strip()

    if m := PRICE.search(stripped):
        raw = m.group(1) or m.group(2)
        value = parse_number(raw)
        if value is not None:
            tight = len(stripped) <= len(m.group(0)) + 3
            yield PatternHit(
                "price", value, 0.75 if tight else 0.5, f"currency pattern {m.group(0)!r}"
            )

    if m := RATING.search(stripped):
        value = parse_number(m.group(1) or m.group(2))
        if value is not None and value <= 5:
            yield PatternHit("rating", value, 0.72, f"rating pattern {m.group(0)!r}")
    elif m := STARS.search(stripped):
        yield PatternHit("rating", float(m.group(0).count("★")), 0.45, "star glyphs")

    if m := DATE_TEXT.search(stripped):
        iso = parse_date(m.group(1))
        if iso:
            tight = len(stripped) <= len(m.group(1)) + 12
            yield PatternHit("date", iso, 0.7 if tight else 0.45, f"date pattern {m.group(1)!r}")
    elif RELATIVE_TIME.search(stripped):
        yield PatternHit("date", stripped, 0.3, "relative time phrase")

    if m := SKU.search(stripped):
        yield PatternHit("sku", m.group(1), 0.7, f"identifier label {m.group(0)!r}")

    if m := EMAIL.search(stripped):
        yield PatternHit("email", m.group(0), 0.8, "email pattern")

    if m := AVAILABILITY.search(stripped):
        yield PatternHit(
            "availability", m.group(1).lower(), 0.6, f"availability phrase {m.group(1)!r}"
        )

    if m := PERCENT.search(stripped):
        value = parse_number(m.group(1))
        if value is not None and len(stripped) <= len(m.group(0)) + 12:
            yield PatternHit("discount", value, 0.4, "percent pattern")

    if len(stripped) <= 12 and (m := DURATION.search(stripped)):
        yield PatternHit("duration", stripped, 0.35, "duration pattern")


def looks_like_price(text: str) -> bool:
    return bool(PRICE.search(text))


def strip_price(text: str) -> tuple[float | None, str | None]:
    m = PRICE.search(text)
    if not m:
        return None, None
    value = parse_number(m.group(1) or m.group(2))
    symbol = re.sub(r"[\d\s.,]", "", m.group(0))
    currency = CURRENCY_SYMBOLS.get(symbol) or (
        symbol.upper() if symbol.upper() in CURRENCY_CODES else None
    )
    return value, currency
