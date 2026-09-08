from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Source(StrEnum):
    """Where a value came from. Ordering here mirrors extraction priority."""

    CONFIG = "config"
    JSONLD = "jsonld"
    MICRODATA = "microdata"
    EMBEDDED_JSON = "embedded_json"
    OPENGRAPH = "opengraph"
    TABLE = "table"
    SEMANTIC = "semantic"
    DOM = "dom"
    ATTRIBUTE = "attribute"
    CLASSNAME = "classname"
    PATTERN = "pattern"
    POSITION = "position"


SOURCE_RANK: dict[Source, int] = {s: i for i, s in enumerate(Source)}


class ValueType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    URL = "url"
    DATE = "date"
    DATETIME = "datetime"
    LIST = "list"
    OBJECT = "object"
    NULL = "null"


@dataclass(slots=True, frozen=True)
class Link:
    url: str
    text: str = ""
    rel: str = ""
    title: str = ""


@dataclass(slots=True, frozen=True)
class Image:
    url: str
    alt: str = ""
    title: str = ""
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class PageModel:
    """Acquisition's only output. Extraction must not care how this was produced."""

    url: str
    html: str = ""
    final_url: str = ""
    status: int | None = None
    title: str | None = None
    text: str = ""
    links: list[Link] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    structured_data: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    acquired_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    elapsed_ms: int = 0
    acquirer: str = ""
    depth: int = 0

    @property
    def base_url(self) -> str:
        return self.final_url or self.url

    def content_hash(self) -> str:
        return hashlib.sha256(self.html.encode("utf-8", "replace")).hexdigest()


@dataclass(slots=True, frozen=True)
class Signal:
    """One additive contribution to a confidence score."""

    name: str
    weight: float
    detail: str = ""


@dataclass(slots=True)
class Candidate:
    name: str
    value: Any
    source: Source
    selector: str | None = None
    confidence: float = 0.0
    signals: list[Signal] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def explain(self) -> str:
        lines = [f"{self.name} = {self.value!r}", f"confidence: {self.confidence:.2f}", "signals:"]
        lines += [
            f"  {s.name:<28} {s.weight:+.2f}" + (f"  # {s.detail}" if s.detail else "")
            for s in self.signals
        ]
        return "\n".join(lines)


@dataclass(slots=True)
class FieldValue:
    name: str
    value: Any
    raw: Any = None
    type: ValueType = ValueType.STRING
    confidence: float = 0.0
    source: Source = Source.DOM
    selector: str | None = None
    signals: list[Signal] = field(default_factory=list)

    def provenance(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "raw": self.raw,
            "type": str(self.type),
            "source": str(self.source),
            "selector": self.selector,
            "confidence": round(self.confidence, 4),
            "signals": [
                {"name": s.name, "weight": s.weight, "detail": s.detail} for s in self.signals
            ],
        }


@dataclass(slots=True)
class Record:
    fields: dict[str, FieldValue] = field(default_factory=dict)
    index: int = 0
    page_url: str = ""
    collection: str = ""
    # The DOM node this record was read from, when there was one. Never serialized.
    node: Any = field(default=None, repr=False, compare=False)

    def __contains__(self, name: str) -> bool:
        return name in self.fields

    def get(self, name: str, default: Any = None) -> Any:
        fv = self.fields.get(name)
        return default if fv is None else fv.value

    def set(self, value: FieldValue) -> None:
        self.fields[value.name] = value

    @property
    def confidence(self) -> float:
        if not self.fields:
            return 0.0
        return sum(f.confidence for f in self.fields.values()) / len(self.fields)

    def to_dict(self, *, provenance: bool = False) -> dict[str, Any]:
        if provenance:
            return {name: fv.provenance() for name, fv in self.fields.items()}
        return {name: fv.value for name, fv in self.fields.items()}

    def identity(self, keys: Iterable[str] | None = None) -> str:
        """Stable hash used by deduplication when no better identity signal exists."""
        payload: Mapping[str, Any]
        if keys:
            payload = {k: self.get(k) for k in sorted(keys)}
        else:
            payload = {k: self.fields[k].value for k in sorted(self.fields)}
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(slots=True)
class CollectionCandidate:
    """A repeated DOM structure that looks like a list of records."""

    selector: str
    count: int
    confidence: float = 0.0
    label: str = ""
    signals: list[Signal] = field(default_factory=list)
    fingerprint: str = ""
    container_selector: str | None = None
    nodes: list[Any] = field(default_factory=list, repr=False, compare=False)


@dataclass(slots=True)
class PaginationHint:
    url: str
    method: str
    confidence: float = 0.0
    selector: str | None = None
    detail: str = ""


@dataclass(slots=True)
class JobStats:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    sources: int = 0
    pages_processed: int = 0
    pages_failed: int = 0
    pages_blocked: int = 0
    records: int = 0
    duplicates: int = 0
    followed: int = 0
    retries: int = 0
    errors_by_code: dict[str, int] = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        end = self.finished_at or datetime.now(UTC)
        return (end - self.started_at).total_seconds()

    def record_error(self, code: str) -> None:
        self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "pages_processed": self.pages_processed,
            "pages_failed": self.pages_failed,
            "pages_blocked": self.pages_blocked,
            "records": self.records,
            "duplicates": self.duplicates,
            "followed": self.followed,
            "retries": self.retries,
            "duration_s": round(self.duration_s, 2),
            "errors_by_code": dict(self.errors_by_code),
        }
