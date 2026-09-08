from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from lxml.html import HtmlElement

from uparse.core.models import Candidate, Signal, Source, ValueType
from uparse.extraction import patterns
from uparse.extraction.dom import SKIP_TAGS
from uparse.extraction.htmlutil import (
    absolutize,
    clean_text,
    image_url,
    node_text,
    relative_css_path,
)
from uparse.extraction.vocabulary import (
    BOILERPLATE,
    FIELD_TYPES,
    field_for_schema_key,
    field_for_token,
    tokens_of,
)
from uparse.processing.normalize import parse_date, parse_number
from uparse.processing.scoring import BONUS, PENALTY, base_signal, score

MAX_TITLE_LEN = 200
MAX_SHORT_FIELD_LEN = 80
# An element that states a price says little else: "£51.77", "Price: £51.77". Once the
# text runs to a sentence the amount is being mentioned, not declared.
MAX_PATTERN_STATEMENT_LEN = 40
_PROSE_UNSAFE = frozenset({"price", "old_price", "rating", "discount", "duration"})
MAX_NODES_PER_RECORD = 400
_ELLIPSIS = ("...", "…", "..")
_FULL_TEXT_ATTRS = ("title", "aria-label", "alt", "data-title", "data-original-title")
_NAME_ATTRS = (
    "itemprop",
    "class",
    "id",
    "data-testid",
    "data-test",
    "data-qa",
    "data-field",
    "aria-label",
)


def from_node(
    node: HtmlElement, base_url: str, *, limit: int = MAX_NODES_PER_RECORD
) -> list[Candidate]:
    """All field candidates found inside one record node, with record-relative selectors."""
    out: list[Candidate] = []
    out += _record_level(node, base_url, node)
    for index, el in enumerate(node.iter()):
        if index > limit:
            break
        if not isinstance(el.tag, str) or el.tag in SKIP_TAGS:
            continue
        out += _from_element(el, base_url, node)
    _demote_containers(out)
    return out


def _record_level(node: HtmlElement, base_url: str, root: HtmlElement) -> list[Candidate]:
    out: list[Candidate] = []
    anchor = _primary_anchor(node)
    if anchor is not None:
        url = absolutize(base_url, anchor.get("href"))
        if url:
            out.append(
                _make(
                    "url",
                    url,
                    Source.SEMANTIC,
                    relative_css_path(anchor, root),
                    [
                        base_signal(Source.SEMANTIC, "record anchor"),
                        Signal("primary link in record", BONUS["semantic_tag"], "<a href>"),
                    ],
                )
            )
    image = next((el for el in node.iter("img") if image_url(el, base_url)), None)
    if image is not None:
        out.append(
            _make(
                "image",
                image_url(image, base_url),
                Source.SEMANTIC,
                relative_css_path(image, root),
                [
                    base_signal(Source.SEMANTIC, "record image"),
                    Signal("first <img> in record", BONUS["semantic_tag"], ""),
                ],
            )
        )
    heading = next(
        (el for el in node.iter("h1", "h2", "h3", "h4") if node_text(el, limit=300)), None
    )
    if heading is not None:
        text = _untruncated(heading, node_text(heading, limit=300))
        signals = [
            base_signal(Source.SEMANTIC, f"<{heading.tag}>"),
            Signal("heading element", BONUS["semantic_tag"], f"<{heading.tag}>"),
        ]
        if len(text) > MAX_TITLE_LEN:
            signals.append(
                Signal("heading text too long", PENALTY["too_long"], f"{len(text)} chars")
            )
        out.append(_make("title", text, Source.SEMANTIC, relative_css_path(heading, root), signals))
    elif anchor is not None:
        text = _untruncated(anchor, node_text(anchor, limit=300))
        if text and text.lower() not in BOILERPLATE:
            out.append(
                _make(
                    "title",
                    text,
                    Source.POSITION,
                    relative_css_path(anchor, root),
                    [base_signal(Source.POSITION, "anchor text fallback")],
                )
            )
    return out


def _untruncated(el: HtmlElement, text: str) -> str:
    """Listings clip visible text but keep the full string in title/alt. Prefer the full one."""
    if not text or not text.rstrip().endswith(_ELLIPSIS):
        return text
    for candidate in (el, *el.iter()):
        if not isinstance(candidate.tag, str):
            continue
        for attr in _FULL_TEXT_ATTRS:
            full = clean_text(candidate.get(attr))
            if full and len(full) > len(text) and not full.rstrip().endswith(_ELLIPSIS):
                return full
    return text


def _primary_anchor(node: HtmlElement) -> HtmlElement | None:
    best: HtmlElement | None = None
    best_score = -1.0
    for a in node.iter("a"):
        href = a.get("href")
        if not href or href.startswith("#"):
            continue
        text = node_text(a, limit=200)
        if text.lower() in BOILERPLATE:
            continue
        weight = len(text) + (30 if len(a.getchildren()) and a.find(".//img") is not None else 0)
        if weight > best_score:
            best, best_score = a, float(weight)
    return best


def _from_element(el: HtmlElement, base_url: str, root: HtmlElement) -> list[Candidate]:
    out: list[Candidate] = []
    selector = relative_css_path(el, root)
    value_text = node_text(el, limit=2000)

    itemprop = el.get("itemprop")
    if itemprop:
        for prop in itemprop.split():
            name = field_for_schema_key(prop)
            if name:
                value = _value_of(el, base_url, want=name) or value_text
                if value and not _implausible(name, value):
                    out.append(
                        _make(
                            name,
                            value,
                            Source.MICRODATA,
                            selector,
                            [
                                base_signal(Source.MICRODATA, f"itemprop={prop}"),
                                Signal("itemprop match", BONUS["name_exact"], prop),
                            ],
                            el,
                        )
                    )

    named = _named_fields(el)
    for name, token, source in named:
        value = _value_of(el, base_url, want=name) or value_text
        if not value or (isinstance(value, str) and value.lower() in BOILERPLATE):
            continue
        if _implausible(name, value):
            continue
        signals = [
            base_signal(source, f"{token!r}"),
            Signal("field name match", BONUS["name_exact"], token),
        ]
        signals += _type_signals(name, value)
        out.append(_make(name, value, source, selector, signals, el))

    out += _semantic(el, base_url, selector)
    out += _patterned(el, value_text, selector)
    return out


# A field name only sticks when the value could actually be one. A class called
# "mobile_comments" is not a phone number just because it contains "mobile".
_SHAPE: dict[str, Callable[[Any], bool]] = {
    "phone": lambda v: (
        sum(c.isdigit() for c in str(v)) >= 7 and patterns.PHONE.search(str(v)) is not None
    ),
    "email": lambda v: patterns.EMAIL.search(str(v)) is not None,
    "price": lambda v: parse_number(v) is not None,
    "old_price": lambda v: parse_number(v) is not None,
    "date": lambda v: parse_date(str(v)) is not None,
    "rating": lambda v: _in_range(parse_number(v), 0, 5),
    "reviews": lambda v: parse_number(v) is not None,
    "views": lambda v: parse_number(v) is not None,
}


def _in_range(value: float | None, low: float, high: float) -> bool:
    return value is not None and low <= value <= high


def _implausible(name: str, value: Any) -> bool:
    check = _SHAPE.get(name)
    return check is not None and not check(value)


def _named_fields(el: HtmlElement) -> list[tuple[str, str, Source]]:
    found: list[tuple[str, str, Source]] = []
    seen: set[str] = set()
    for attr in _NAME_ATTRS:
        raw = el.get(attr)
        if not raw:
            continue
        source = Source.CLASSNAME if attr in {"class", "id"} else Source.ATTRIBUTE
        for token in tokens_of(raw):
            name = field_for_token(token)
            if name and name not in seen:
                seen.add(name)
                found.append((name, token, source))
    for attr in el.attrib:
        if not attr.startswith("data-"):
            continue
        name = field_for_token(attr.removeprefix("data-"))
        if name and name not in seen:
            seen.add(name)
            found.append((name, attr, Source.ATTRIBUTE))
    return found


def _semantic(el: HtmlElement, base_url: str, selector: str) -> list[Candidate]:
    match el.tag:
        case "time":
            raw = el.get("datetime") or node_text(el, limit=120)
            iso = parse_date(raw)
            if iso:
                return [
                    _make(
                        "date",
                        iso,
                        Source.SEMANTIC,
                        selector,
                        [
                            base_signal(Source.SEMANTIC, "<time>"),
                            Signal("semantic element", BONUS["semantic_tag"], "<time datetime>"),
                        ],
                    )
                ]
        case "img":
            url = image_url(el, base_url)
            if url:
                return [
                    _make("image", url, Source.DOM, selector, [base_signal(Source.DOM, "<img>")])
                ]
        case "address":
            text = node_text(el, limit=400)
            if text:
                return [
                    _make(
                        "location",
                        text,
                        Source.SEMANTIC,
                        selector,
                        [
                            base_signal(Source.SEMANTIC, "<address>"),
                            Signal("semantic element", BONUS["semantic_tag"], "<address>"),
                        ],
                    )
                ]
        case "meta":
            return []
    return []


def _patterned(el: HtmlElement, text: str, selector: str) -> list[Candidate]:
    if not text or len(text) > 300 or len(el.getchildren()) > 4:
        return []
    out: list[Candidate] = []
    prose = len(text) > MAX_PATTERN_STATEMENT_LEN
    for hit in patterns.detect(text):
        if prose and hit.field in _PROSE_UNSAFE:
            continue
        out.append(
            _make(
                hit.field,
                hit.value,
                Source.PATTERN,
                selector,
                [
                    base_signal(Source.PATTERN, hit.detail),
                    Signal("text pattern", round(hit.confidence * 0.3, 3), hit.detail),
                ],
            )
        )
    return out


def _type_signals(name: str, value: Any) -> list[Signal]:
    expected = FIELD_TYPES.get(name)
    if expected is None:
        return []
    text = str(value)
    match name:
        case "price" | "old_price" | "rating" | "discount" | "reviews" | "views":
            return (
                [Signal("value parses as a number", BONUS["type_agrees"], text[:40])]
                if parse_number(value) is not None
                else [Signal("value is not numeric", PENALTY["too_short"], text[:40])]
            )
        case "date":
            return (
                [Signal("value parses as a date", BONUS["type_agrees"], text[:40])]
                if parse_date(value)
                else []
            )
        case "url" | "image":
            return (
                [Signal("value is a URL", BONUS["type_agrees"], text[:60])]
                if text.startswith(("http", "//", "/"))
                else []
            )
        case "title":
            if len(text) > MAX_TITLE_LEN:
                return [Signal("title too long", PENALTY["too_long"], f"{len(text)} chars")]
            if len(text) < 2:
                return [Signal("title too short", PENALTY["too_short"], repr(text))]
        case (
            "author"
            | "brand"
            | "category"
            | "currency"
            | "sku"
            | "company"
            | "availability"
            | "location"
        ):
            # These name one short thing. A long value means the container was grabbed.
            if len(text) > MAX_SHORT_FIELD_LEN:
                return [
                    Signal(
                        "value too long for this field", PENALTY["too_long"], f"{len(text)} chars"
                    )
                ]
    return []


def _value_of(el: HtmlElement, base_url: str, *, want: str = "") -> Any:
    match el.tag:
        case "a" | "link" | "area":
            # An anchor named as a text field ("name", "title") means its text, not its href.
            if want and FIELD_TYPES.get(want) not in (ValueType.URL, None):
                return node_text(el, limit=2000)
            return absolutize(base_url, el.get("href"))
        case "img":
            return image_url(el, base_url)
        case "meta":
            return el.get("content")
        case "time":
            return el.get("datetime") or node_text(el, limit=120)
        case "data" | "meter" | "input":
            return el.get("value") or el.get("content")
        case _:
            content = el.get("content") or el.get("data-value")
            return content if content else None


def _make(
    name: str,
    value: Any,
    source: Source,
    selector: str,
    signals: list[Signal],
    el: HtmlElement | None = None,
) -> Candidate:
    cand = Candidate(
        name=name,
        value=value,
        source=source,
        selector=selector,
        signals=signals,
        metadata={"el": el} if el is not None else {},
    )
    score(cand)
    return cand


def _demote_containers(candidates: list[Candidate]) -> None:
    """When two candidates name the same field, the one wrapping the other is the label.

    `div.byline` and the `<a>` inside it both look like an author; the wrapper's text is
    the whole line. Preferring the inner element is what stops values swallowing rows.
    """
    by_name: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.metadata.get("el") is not None:
            by_name[candidate.name].append(candidate)
    for group in by_name.values():
        if len(group) < 2:
            continue
        elements = [c.metadata["el"] for c in group]
        for candidate, element in zip(group, elements, strict=True):
            if any(other is not element and _contains(element, other) for other in elements):
                candidate.signals.append(
                    Signal("wraps a more specific value", PENALTY["boilerplate"], "container")
                )
                score(candidate)


def _contains(ancestor: HtmlElement, node: HtmlElement) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.getparent()
    return False


def from_schema_object(obj: dict[str, Any], source: Source, base_url: str = "") -> list[Candidate]:
    """Map a JSON-LD / microdata / OpenGraph object onto canonical fields."""
    out: list[Candidate] = []
    for key, raw in _flatten(obj):
        name = field_for_schema_key(key)
        if not name:
            continue
        value = _scalar(raw)
        if value in (None, "", []) or _implausible(name, value):
            continue
        if name in {"url", "image"} and isinstance(value, str):
            value = absolutize(base_url, value) or value
        signals = [
            base_signal(source, f"{key}"),
            Signal("structured field name", BONUS["name_exact"], key),
        ]
        signals += _type_signals(name, value)
        out.append(_make(name, value, source, f"{source}:{key}", signals))
    return out


def _flatten(obj: dict[str, Any], prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    """Nested schema objects (offers, aggregateRating, brand) are flattened onto their keys."""
    out: list[tuple[str, Any]] = []
    if depth > 3:
        return out
    for key, value in obj.items():
        if key.startswith("@") and key != "@id":
            continue
        if isinstance(value, dict):
            out += _flatten(value, prefix=key, depth=depth + 1)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            out += _flatten(value[0], prefix=key, depth=depth + 1)
        else:
            out.append((key, value))
    del prefix
    return out


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        return _scalar(value[0]) if value else None
    if isinstance(value, dict):
        for key in ("name", "url", "value", "@id"):
            if key in value:
                return _scalar(value[key])
        return None
    return value


def group_by_field(candidates: list[Candidate]) -> dict[str, list[Candidate]]:
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        grouped[c.name].append(c)
    return dict(grouped)
