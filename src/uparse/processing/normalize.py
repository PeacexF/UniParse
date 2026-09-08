from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from dateutil import parser as dateparser

from uparse.core.models import ValueType

_WS = re.compile(r"\s+")
_SPACES = "       "
_NUM = re.compile(r"[-+]?\d[\d\s.,  ']*\d|\d")

CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "USD",
    "US$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₽": "RUB",
    "₹": "INR",
    "₩": "KRW",
    "₴": "UAH",
    "₺": "TRY",
    "R$": "BRL",
    "C$": "CAD",
    "A$": "AUD",
    "CHF": "CHF",
    "zł": "PLN",
    "Kč": "CZK",
    "kr": "SEK",
}
CURRENCY_CODES = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "RUB",
        "INR",
        "KRW",
        "UAH",
        "TRY",
        "BRL",
        "CAD",
        "AUD",
        "CHF",
        "PLN",
        "CZK",
        "SEK",
        "NOK",
        "DKK",
        "CNY",
        "MXN",
    }
)

TRUTHY = frozenset({"true", "yes", "y", "1", "in stock", "instock", "available", "on", "enabled"})
FALSY = frozenset(
    {
        "false",
        "no",
        "n",
        "0",
        "out of stock",
        "outofstock",
        "unavailable",
        "off",
        "disabled",
        "sold out",
    }
)

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFC", value)
    for ch in _SPACES:
        value = value.replace(ch, " ")
    return _WS.sub(" ", value).strip()


def normalize_url(value: str | None, base_url: str = "") -> str:
    text = normalize_text(value)
    if not text:
        return ""
    if base_url:
        text = urljoin(base_url, text)
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https", "file"}:
        return ""
    return text


def parse_number(value: Any) -> float | None:
    """Locale-tolerant number parsing.

    A single separator followed by exactly three digits is read as grouping,
    so "1.299" and "1,299" both mean 1299. "12.99" and "12,99" mean 12.99.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = normalize_text(str(value))
    if not text:
        return None
    match = _NUM.search(text.replace("'", ""))
    if match is None:
        return None
    raw = match.group(0)
    sign = -1.0 if raw.lstrip().startswith("-") else 1.0
    digits = raw.lstrip("+-")
    seps = [c for c in digits if c in {".", ",", " "}]
    body = digits
    if not seps:
        return _to_float(body, sign)
    distinct = set(seps)
    if len(distinct) > 1:
        decimal_sep = digits[max(digits.rfind(c) for c in distinct)]
        body = "".join(c for c in digits if c.isdigit() or c == decimal_sep)
        body = body.replace(decimal_sep, ".")
        return _to_float(body, sign)
    sep = seps[0]
    if sep == " ":
        return _to_float(digits.replace(" ", ""), sign)
    if len(seps) > 1:
        return _to_float(digits.replace(sep, ""), sign)
    tail = digits.split(sep)[-1]
    if len(tail) == 3 and len(digits.split(sep)[0]) <= 3:
        return _to_float(digits.replace(sep, ""), sign)
    return _to_float(digits.replace(sep, "."), sign)


def _to_float(body: str, sign: float) -> float | None:
    try:
        return sign * float(body)
    except ValueError:
        return None


def parse_currency(value: str) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    upper = text.upper()
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            return code
    for symbol, code in sorted(CURRENCY_SYMBOLS.items(), key=lambda kv: -len(kv[0])):
        if symbol in text:
            return code
    return None


def parse_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = normalize_text(str(value)).lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return None


def parse_date(value: Any) -> str | None:
    """Return an ISO-8601 string, or None when parsing is not confident."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = normalize_text(str(value))
    if not text or len(text) > 64:
        return None
    if ISO_DATE.match(text):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    if not re.search(r"\d", text):
        return None
    try:
        parsed = dateparser.parse(text, fuzzy=False)
    except ValueError, OverflowError, TypeError:
        return None
    # A bare "12" parses to today's date with the day replaced; reject that.
    if len(re.sub(r"\D", "", text)) < 4:
        return None
    return parsed.isoformat()


def coerce(value: Any, target: ValueType, *, base_url: str = "") -> tuple[Any, ValueType]:
    """Convert when confident, otherwise return the original untouched."""
    if value is None:
        return None, ValueType.NULL
    match target:
        case ValueType.NUMBER:
            num = parse_number(value)
            return (num, ValueType.NUMBER) if num is not None else (value, ValueType.STRING)
        case ValueType.INTEGER:
            num = parse_number(value)
            if num is None:
                return value, ValueType.STRING
            return int(num), ValueType.INTEGER
        case ValueType.BOOLEAN:
            flag = parse_boolean(value)
            return (flag, ValueType.BOOLEAN) if flag is not None else (value, ValueType.STRING)
        case ValueType.URL:
            url = normalize_url(str(value), base_url)
            return (url, ValueType.URL) if url else (value, ValueType.STRING)
        case ValueType.DATE | ValueType.DATETIME:
            iso = parse_date(value)
            return (iso, target) if iso else (value, ValueType.STRING)
        case ValueType.STRING:
            return normalize_text(str(value)), ValueType.STRING
        case _:
            return value, infer_type(value)


SCHEMA_ENUM = re.compile(r"^https?://schema\.org/(\w+)$", re.I)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def normalize_enum(value: Any) -> Any:
    """schema.org enum URLs (…/InStock) become plain lowercase phrases."""
    if not isinstance(value, str):
        return value
    m = SCHEMA_ENUM.match(value.strip())
    if not m:
        return value
    return _CAMEL.sub(" ", m.group(1)).lower()


def infer_type(value: Any) -> ValueType:
    if value is None:
        return ValueType.NULL
    if isinstance(value, bool):
        return ValueType.BOOLEAN
    if isinstance(value, int):
        return ValueType.INTEGER
    if isinstance(value, float):
        return ValueType.NUMBER
    if isinstance(value, list):
        return ValueType.LIST
    if isinstance(value, dict):
        return ValueType.OBJECT
    text = str(value)
    if text.startswith(("http://", "https://")):
        return ValueType.URL
    return ValueType.STRING
