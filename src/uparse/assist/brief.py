"""The page summary a model is asked to reason about.

Not the HTML. The engine has already found the structure and validated it against every
record; what it cannot always do is *name* things. A brief is that finding, compressed —
typically 2-4 KB against a page of 50-600 KB — so a small free-tier model can hold the
whole problem at once.

Every candidate carries a short id (`c1`, `c2`, ...). Proposals cite ids, never selectors,
so an invented selector is not something a model is able to express.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from lxml.html import HtmlElement

from uparse.core.models import PageModel
from uparse.extraction import alignment as align
from uparse.extraction.engine import ExtractionResult
from uparse.extraction.htmlutil import node_text

MAX_COLUMNS = 20
MAX_COLLECTIONS = 4
MAX_SAMPLES = 3
SAMPLE_CHARS = 70
MAX_FIELD_SAMPLE = 60


@dataclass(slots=True)
class ColumnBrief:
    """One thing that repeats across the records, named or not."""

    id: str
    selector: str
    coverage: float
    samples: list[str]
    named: str | None = None
    confidence: float | None = None


@dataclass(slots=True)
class CollectionBrief:
    id: str
    selector: str
    count: int
    confidence: float
    label: str
    chosen: bool = False


@dataclass(slots=True)
class PageBrief:
    url: str
    title: str | None
    strategy: str
    record_count: int
    collections: list[CollectionBrief] = field(default_factory=list)
    columns: list[ColumnBrief] = field(default_factory=list)
    structured_data: dict[str, int] = field(default_factory=dict)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=indent, default=str)

    def column(self, column_id: str) -> ColumnBrief | None:
        return next((c for c in self.columns if c.id == column_id), None)

    def collection(self, collection_id: str) -> CollectionBrief | None:
        return next((c for c in self.collections if c.id == collection_id), None)

    @property
    def has_material(self) -> bool:
        """False when there is nothing for a model to work with."""
        return bool(self.columns)


def build(page: PageModel, result: ExtractionResult) -> PageBrief:
    brief = PageBrief(
        url=page.base_url,
        title=page.title,
        strategy=result.strategy,
        record_count=len(result.records),
        structured_data=dict(result.structured_counts),
    )

    for index, candidate in enumerate(result.collections[:MAX_COLLECTIONS], 1):
        brief.collections.append(
            CollectionBrief(
                id=f"g{index}",
                selector=candidate.selector,
                count=candidate.count,
                confidence=round(candidate.confidence, 3),
                label=candidate.label,
                chosen=candidate is result.collection,
            )
        )

    brief.columns = _columns(result)
    return brief


def _columns(result: ExtractionResult) -> list[ColumnBrief]:
    """Named fields first — they anchor the model — then everything left unnamed."""
    out: list[ColumnBrief] = []
    counter = 0

    for name, (vtype, confidence) in result.schema.items():
        counter += 1
        samples = [
            _clip(record.get(name), MAX_FIELD_SAMPLE)
            for record in result.records[:MAX_SAMPLES]
            if record.get(name) not in (None, "")
        ]
        selector = next(
            (
                r.fields[name].selector
                for r in result.records
                if name in r.fields and r.fields[name].selector
            ),
            "",
        )
        out.append(
            ColumnBrief(
                id=f"c{counter}",
                selector=selector or f"(field:{name})",
                coverage=_coverage(result, name),
                samples=samples,
                named=name,
                confidence=confidence,
            )
        )
        del vtype

    known = {c.selector for c in out}
    for column in _unnamed(result):
        if column.selector in known or counter >= MAX_COLUMNS:
            continue
        counter += 1
        out.append(
            ColumnBrief(
                id=f"c{counter}",
                selector=column.selector,
                coverage=round(column.coverage, 2),
                samples=[_clip(t, SAMPLE_CHARS) for t in column.texts[:MAX_SAMPLES] if t],
            )
        )
    return out


def _unnamed(result: ExtractionResult) -> list[align.Column]:
    """Columns alignment can see but could not name. This is the gap the model fills."""
    if result.collection is None or not result.collection.nodes:
        return []
    nodes: list[HtmlElement] = result.collection.nodes
    return [c for c in align.columns_of(nodes) if align.name_of(c) is None and c.avg_len >= 2]


def _coverage(result: ExtractionResult, name: str) -> float:
    if not result.records:
        return 0.0
    present = sum(1 for r in result.records if r.get(name) not in (None, ""))
    return round(present / len(result.records), 2)


def _clip(value: object, limit: int) -> str:
    text = node_text_of(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def node_text_of(value: object) -> str:
    if isinstance(value, HtmlElement):
        return node_text(value, limit=400)
    return str(value)
