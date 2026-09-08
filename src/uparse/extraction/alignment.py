"""Field discovery from what repeats across records, not from what a class name is called.

The vocabulary in `fields.py` only sees a field when a site names it conventionally, so a
page built with CSS-in-JS or utility classes degrades to url + title. Alignment looks at a
collection as a table instead: a relative selector that resolves in most records is a
column, and a column whose values all parse the same way is a typed field.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from statistics import mean

from lxml.html import HtmlElement

from uparse.core.models import Candidate, Signal, Source
from uparse.extraction.dom import SKIP_TAGS, stable_classes
from uparse.extraction.htmlutil import node_text, relative_css_path
from uparse.extraction.patterns import PRICE
from uparse.extraction.vocabulary import BOILERPLATE, TOKEN_TO_FIELD
from uparse.processing.normalize import parse_date, parse_number
from uparse.processing.scoring import BONUS, base_signal, score

MIN_RECORDS = 3
MIN_COVERAGE = 0.6
MAX_NODES_PER_RECORD = 400
MAX_COLUMNS = 40
MAX_DESCENDANTS = 6
WRAPPER_SHARE = 0.75
LONG_TEXT = 60
MAX_NAMED_LEN = 120
Predicate = Callable[[str], bool]
_NAME_OK = re.compile(r"[a-z][a-z0-9]{2,23}")
_SPLIT = re.compile(r"[-_]")
# Layout and framework noise that names a box, never a value.
_GENERIC = frozenset(
    # Layout, spacing and typography utilities. Tailwind-style classes are namespaced
    # ("font-semibold"), so the specific half needs rejecting as firmly as the generic one.
    {
        "col",
        "cols",
        "column",
        "row",
        "container",
        "wrapper",
        "wrap",
        "inner",
        "outer",
        "content",
        "contents",
        "box",
        "block",
        "body",
        "main",
        "text",
        "value",
        "field",
        "item",
        "items",
        "card",
        "cell",
        "entry",
        "list",
        "group",
        "section",
        "left",
        "right",
        "top",
        "bottom",
        "center",
        "middle",
        "small",
        "large",
        "md",
        "lg",
        "sm",
        "xs",
        "xl",
        "flex",
        "grid",
        "info",
        "meta",
        "data",
        "detail",
        "details",
        "hidden",
        "visible",
        "active",
        "first",
        "last",
        "even",
        "odd",
        "js",
        "is",
        "has",
        "u",
        "p",
        "e",
        "h",
        "label",
        "btn",
        "button",
        "icon",
        "liner",
        "separator",
        "sep",
        "spacer",
        "toggle",
        "badge",
        "holder",
        "placeholder",
        # typography
        "font",
        "bold",
        "semibold",
        "medium",
        "normal",
        "light",
        "regular",
        "italic",
        "underline",
        "uppercase",
        "lowercase",
        "capitalize",
        "condensed",
        "expanded",
        "mono",
        "serif",
        "sans",
        "size",
        "weight",
        "leading",
        "tracking",
        "truncate",
        "ellipsis",
        "nowrap",
        "align",
        "justify",
        "muted",
        "subtle",
        "strong",
        "emphasis",
        # colour and state
        "primary",
        "secondary",
        "success",
        "danger",
        "warning",
        "dark",
        "white",
        "black",
        "gray",
        "grey",
        "hover",
        "focus",
        "disabled",
        "selected",
        "current",
        "open",
        # positioning and spacing
        "inline",
        "relative",
        "absolute",
        "fixed",
        "sticky",
        "float",
        "clear",
        "rounded",
        "border",
        "shadow",
        "opacity",
        "overflow",
        "gap",
        "space",
        "auto",
        "none",
        "full",
        "half",
        "start",
        "end",
        "min",
        "max",
        "width",
        "height",
        "margin",
        "padding",
        "mt",
        "mb",
        "ml",
        "mr",
        "pt",
        "pb",
        "pl",
        "pr",
        "px",
        "py",
        "mx",
        "my",
        "sr",
        "snug",
        "tight",
        "loose",
        "relaxed",
        "wide",
        "wider",
        "widest",
        "baseline",
        "only",
        "screen",
        "print",
        "transition",
        "transform",
        "scale",
        "cursor",
    }
)


@dataclass(slots=True)
class Column:
    """One relative selector, seen across every record of a collection."""

    selector: str
    texts: list[str]
    coverage: float
    depth: int
    sample: HtmlElement | None = None

    @property
    def distinct(self) -> float:
        filled = [t for t in self.texts if t]
        return len(set(filled)) / len(filled) if filled else 0.0

    @property
    def avg_len(self) -> float:
        filled = [t for t in self.texts if t]
        return mean(len(t) for t in filled) if filled else 0.0

    def ratio(self, predicate: Predicate) -> float:
        filled = [t for t in self.texts if t]
        if not filled:
            return 0.0
        return sum(1 for t in filled if predicate(t)) / len(filled)


def candidates(nodes: list[HtmlElement], base_url: str) -> list[list[Candidate]]:
    """Per-record candidates derived from columns the whole collection shares."""
    del base_url  # values are text here; URL-valued fields come from the semantic pass
    if len(nodes) < MIN_RECORDS:
        return [[] for _ in nodes]

    per_record = [_paths(node) for node in nodes]
    out: list[list[Candidate]] = [[] for _ in nodes]
    named = 0
    for column in _columns(per_record):
        if named >= MAX_COLUMNS:
            break
        naming = _name(column)
        if naming is None:
            continue
        name, reason = naming
        named += 1
        for index, texts in enumerate(per_record):
            element = texts.get(column.selector)
            if element is None:
                continue
            value = node_text(element, limit=4000)
            if not value:
                continue
            out[index].append(_make(name, value, column, reason))
    return out


def _paths(node: HtmlElement) -> dict[str, HtmlElement]:
    """Relative selector -> element, for the parts of a record worth treating as a column."""
    found: dict[str, HtmlElement] = {}
    whole = len(node_text(node, limit=8000)) or 1
    for index, el in enumerate(node.iter()):
        if index > MAX_NODES_PER_RECORD:
            break
        if el is node or not isinstance(el.tag, str) or el.tag in SKIP_TAGS:
            continue
        if sum(1 for _ in el.iterdescendants()) > MAX_DESCENDANTS:
            continue  # a wrapper, not a value: taking it would swallow the whole record
        if len(node_text(el, limit=8000)) / whole > WRAPPER_SHARE:
            continue  # restates the record rather than naming one part of it
        found.setdefault(relative_css_path(el, node), el)
    return found


def _columns(per_record: list[dict[str, HtmlElement]]) -> list[Column]:
    total = len(per_record)
    counts: dict[str, int] = {}
    for record in per_record:
        for selector in record:
            counts[selector] = counts.get(selector, 0) + 1

    columns: list[Column] = []
    for selector, seen in counts.items():
        coverage = seen / total
        if coverage < MIN_COVERAGE:
            continue
        texts = [
            node_text(record[selector], limit=1000) if selector in record else ""
            for record in per_record
        ]
        if not any(texts):
            continue
        columns.append(
            Column(
                selector=selector,
                texts=texts,
                coverage=coverage,
                depth=selector.count(">"),
                sample=next((r[selector] for r in per_record if selector in r), None),
            )
        )
    # Deepest first: the innermost element holding a value beats its wrapper.
    columns.sort(key=lambda c: (-c.depth, -c.coverage))
    return columns


def _name(column: Column) -> tuple[str, Signal] | None:
    """Name a column from the shape of its values. Silence beats a guessed name."""
    if column.avg_len < 2 or _is_boilerplate(column):
        return None

    if column.ratio(_is_date) >= 0.8:
        return "date", Signal(
            "column parses as dates", BONUS["type_agrees"], _pct(column, _is_date)
        )

    if column.ratio(_is_money) >= 0.8:
        return "price", Signal(
            "column parses as money", BONUS["type_agrees"], _pct(column, _is_money)
        )

    if column.avg_len >= LONG_TEXT and column.distinct >= 0.8:
        return "description", Signal(
            "longest varying column", BONUS["name_partial"], f"{column.avg_len:.0f} chars/record"
        )

    named = _name_from_markup(column)
    if named is not None:
        return named, Signal("column named by the page", BONUS["name_partial"], named)

    return None


def _name_from_markup(column: Column) -> str | None:
    """Sites name their own fields. `span.country-capital` is a capital column.

    The canonical vocabulary only covers fields common enough to have a shared name; this
    picks up everything else, which is most of what an unfamiliar site actually contains.
    """
    if column.sample is None or column.coverage < 0.8 or column.distinct < 0.5:
        return None
    if column.avg_len > MAX_NAMED_LEN:
        return None
    for token, explicit in _tokens(column.sample):
        raw = token.lower()
        # A namespaced class ("country-capital") names data; a bare one is as likely to be
        # typography ("semibold") or a CSS-module hash ("fifkvi"). Only an authored
        # attribute — itemprop, data-field — is trusted on its own.
        if not explicit and not _SPLIT.search(raw):
            continue
        parts = [p for p in _SPLIT.split(raw) if p and p not in _GENERIC]
        if not parts:
            continue
        name = parts[-1]
        if _NAME_OK.fullmatch(name) and name not in TOKEN_TO_FIELD:
            return name
    return None


def _tokens(el: HtmlElement) -> list[tuple[str, bool]]:
    """Candidate names for this element, flagged with whether the page authored them."""
    out: list[tuple[str, bool]] = []
    for attr in ("itemprop", "data-field", "data-testid", "data-test"):
        value = el.get(attr)
        if value:
            out.append((value.strip(), True))
    out += [(t, False) for t in sorted(stable_classes(el)) if t]
    return out


def _make(name: str, value: str, column: Column, reason: Signal) -> Candidate:
    candidate = Candidate(
        name=name,
        value=value,
        source=Source.POSITION,
        selector=column.selector,
        signals=[
            base_signal(Source.POSITION, "cross-record alignment"),
            Signal(
                "aligned across records", BONUS["consistent"], f"{column.coverage:.0%} of items"
            ),
            reason,
        ],
    )
    score(candidate)
    return candidate


def _is_boilerplate(column: Column) -> bool:
    """A column repeating one UI label in every record is chrome, not data."""
    filled = [t for t in column.texts if t]
    if not filled:
        return True
    if column.distinct > 0.25:
        return False
    return filled[0].strip().lower() in BOILERPLATE or len(filled[0]) < 24


def _is_date(text: str) -> bool:
    return len(text) <= 60 and parse_date(text) is not None


def _is_money(text: str) -> bool:
    return len(text) <= 40 and parse_number(text) is not None and PRICE.search(text) is not None


def _pct(column: Column, predicate: Predicate) -> str:
    return f"{column.ratio(predicate):.0%} of values"
