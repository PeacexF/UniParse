from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from lxml.html import HtmlElement

from uparse.config.schema import ExtractionConfig, FieldSpec
from uparse.core.models import (
    SOURCE_RANK,
    Candidate,
    CollectionCandidate,
    FieldValue,
    PageModel,
    Record,
    Signal,
    Source,
    ValueType,
)
from uparse.extraction import collections as coll
from uparse.extraction import fields as fld
from uparse.extraction import structured as sd
from uparse.extraction import tables as tbl
from uparse.extraction.htmlutil import absolutize, hydrate, node_text, parse, select
from uparse.extraction.vocabulary import FIELD_TYPES
from uparse.processing.normalize import coerce, normalize_enum
from uparse.processing.scoring import apply_consistency, base_signal, best_per_field, score

Strategy = str


@dataclass(slots=True)
class ExtractionResult:
    records: list[Record] = dc_field(default_factory=list)
    strategy: Strategy = "none"
    collection: CollectionCandidate | None = None
    collections: list[CollectionCandidate] = dc_field(default_factory=list)
    structured_counts: Counter[str] = dc_field(default_factory=Counter)
    schema: dict[str, tuple[ValueType, float]] = dc_field(default_factory=dict)
    root: HtmlElement | None = dc_field(default=None, repr=False)
    notes: list[str] = dc_field(default_factory=list)


def extract(page: PageModel, config: ExtractionConfig | None = None) -> ExtractionResult:
    cfg = config or ExtractionConfig()
    root = parse(page.html, page.base_url)
    hydrate(page, root)
    result = ExtractionResult(root=root)

    blocks = sd.extract_all(root, page.base_url) if cfg.include_structured_data else []
    page.structured_data = [b["_data"] for b in blocks if b["_source"] in {"jsonld", "microdata"}]
    result.structured_counts = _count_types(blocks)

    result.collections = coll.discover(root, min_records=cfg.min_records)

    records = _from_structured(blocks, page, cfg)
    if records:
        result.strategy = "structured"
    if not records and cfg.include_tables:
        records = tbl.extract(root, page.base_url, min_rows=cfg.min_records)
        if records:
            result.strategy = "table"
    if not records:
        records, chosen = _from_dom(root, page, cfg, result.collections)
        result.collection = chosen
        if records:
            result.strategy = "dom"
    if not records:
        records = _single_record(blocks, page, cfg)
        if records:
            result.strategy = "page"

    _apply_config_fields(records, root, page, cfg)
    _finalize(records, page, cfg)

    if cfg.min_confidence > 0:
        records = [r for r in records if r.confidence >= cfg.min_confidence]
    result.records = records[: cfg.max_records_per_page]
    result.schema = _schema(result.records)
    return result


# ------------------------------------------------------------- strategies


def _from_structured(
    blocks: list[dict[str, Any]], page: PageModel, cfg: ExtractionConfig
) -> list[Record]:
    # Grouped by (source, type): a JSON-LD Product and a microdata Product on a detail
    # page describe one entity, not two records.
    groups: dict[tuple[Source, str], list[dict[str, Any]]] = {}
    for block in blocks:
        source = _source_of(block["_source"])
        if source not in {Source.JSONLD, Source.MICRODATA}:
            continue
        obj = block["_data"]
        if not isinstance(obj, dict) or not sd.is_record_type(obj):
            continue
        groups.setdefault((source, ",".join(sd.type_names(obj))), []).append(obj)

    if not groups:
        return []
    (source, key), items = max(groups.items(), key=lambda kv: (len(kv[1]), -SOURCE_RANK[kv[0][0]]))
    if len(items) < cfg.min_records:
        return []
    return [
        _record_from_candidates(
            fld.from_schema_object(obj, source, page.base_url), index, page, key
        )
        for index, obj in enumerate(items)
    ]


def _from_dom(
    root: HtmlElement, page: PageModel, cfg: ExtractionConfig, candidates: list[CollectionCandidate]
) -> tuple[list[Record], CollectionCandidate | None]:
    chosen: CollectionCandidate | None = None
    if cfg.collection:
        nodes = select(root, cfg.collection)
        if nodes:
            chosen = CollectionCandidate(
                selector=cfg.collection,
                count=len(nodes),
                confidence=1.0,
                label="configured",
                signals=[Signal("configured selector", 1.0, cfg.collection)],
                nodes=nodes,
            )
    elif candidates:
        chosen = candidates[0]

    if chosen is None or not chosen.nodes:
        return [], None

    per_record = [fld.from_node(node, page.base_url, root) for node in chosen.nodes]
    grouped = [fld.group_by_field(c) for c in per_record]
    apply_consistency(grouped)
    records = [
        _record_from_candidates(cands, index, page, chosen.label)
        for index, cands in enumerate(per_record)
    ]
    return [r for r in records if r.fields], chosen


def _single_record(
    blocks: list[dict[str, Any]], page: PageModel, cfg: ExtractionConfig
) -> list[Record]:
    cands: list[Candidate] = []
    for block in blocks:
        source = _source_of(block["_source"])
        obj = block["_data"]
        if not isinstance(obj, dict):
            continue
        if source is Source.EMBEDDED_JSON:
            continue
        cands += fld.from_schema_object(obj, source, page.base_url)
    if page.title:
        cands.append(
            Candidate(
                name="title",
                value=page.title,
                source=Source.POSITION,
                selector="title",
                signals=[base_signal(Source.POSITION, "<title>")],
            )
        )
    cands.append(
        Candidate(
            name="url",
            value=page.base_url,
            source=Source.SEMANTIC,
            selector="@location",
            signals=[base_signal(Source.SEMANTIC, "page URL"), Signal("page identity", 0.3, "")],
        )
    )
    for c in cands:
        score(c)
    record = _record_from_candidates(cands, 0, page, "page")
    return [record] if len(record.fields) > 1 else []


# --------------------------------------------------------- config overrides


def _apply_config_fields(
    records: list[Record], root: HtmlElement, page: PageModel, cfg: ExtractionConfig
) -> None:
    """Explicit configuration always wins; it runs last and overwrites inference."""
    if not cfg.fields:
        return
    nodes = [None] * len(records)
    for name, spec in cfg.fields.items():
        if spec.mode == "auto":
            continue
        for index, record in enumerate(records):
            scope = nodes[index] if nodes[index] is not None else root
            value = _apply_spec(spec, scope, page)
            if value is None and spec.default is not None:
                value = spec.default
            if value is None:
                record.fields.pop(name, None)
                continue
            vtype = (
                ValueType(spec.type)
                if spec.type != "auto"
                else FIELD_TYPES.get(name, ValueType.STRING)
            )
            coerced, actual = coerce(value, vtype, base_url=page.base_url)
            record.set(
                FieldValue(
                    name=name,
                    value=coerced,
                    raw=value,
                    type=actual,
                    confidence=1.0,
                    source=Source.CONFIG,
                    selector=spec.selector,
                    signals=[Signal("explicit configuration", 1.0, spec.mode)],
                )
            )


def _apply_spec(spec: FieldSpec, scope: HtmlElement, page: PageModel) -> Any:
    match spec.mode:
        case "constant":
            return spec.value
        case "regex":
            import re

            found = re.search(spec.regex or "", page.text or page.html)
            return (found.group(1) if found.groups() else found.group(0)) if found else None
        case "attribute":
            found_nodes = select(scope, spec.selector or "")
            if not found_nodes:
                return None
            raw = found_nodes[0].get(spec.attribute or "")
            if spec.many:
                return [n.get(spec.attribute or "") for n in found_nodes]
            return raw
        case "selector":
            found_nodes = select(scope, spec.selector or "")
            if not found_nodes:
                return None
            if spec.many:
                return [node_text(n, limit=2000) for n in found_nodes]
            node = found_nodes[0]
            if node.tag == "a" and node.get("href"):
                return absolutize(page.base_url, node.get("href"))
            if node.tag == "img":
                return absolutize(page.base_url, node.get("src"))
            return node_text(node, limit=4000)
        case _:
            return None


# ------------------------------------------------------------- finishing


def _record_from_candidates(
    candidates: list[Candidate], index: int, page: PageModel, collection: str
) -> Record:
    record = Record(index=index, page_url=page.base_url, collection=collection)
    for name, cand in best_per_field(candidates).items():
        record.set(
            FieldValue(
                name=name,
                value=cand.value,
                raw=cand.value,
                type=FIELD_TYPES.get(name, ValueType.STRING),
                confidence=cand.confidence,
                source=cand.source,
                selector=cand.selector,
                signals=cand.signals,
            )
        )
    return record


def _finalize(records: list[Record], page: PageModel, cfg: ExtractionConfig) -> None:
    wanted = set(cfg.fields) if cfg.fields else None
    for record in records:
        for name in list(record.fields):
            if wanted is not None and name not in wanted:
                del record.fields[name]
                continue
            fv = record.fields[name]
            if fv.source is Source.CONFIG:
                continue
            target = FIELD_TYPES.get(name, ValueType.STRING)
            value, actual = coerce(normalize_enum(fv.value), target, base_url=page.base_url)
            fv.value, fv.type = value, actual
            if value in (None, ""):
                del record.fields[name]


def _schema(records: list[Record]) -> dict[str, tuple[ValueType, float]]:
    if not records:
        return {}
    totals: dict[str, list[float]] = {}
    types: dict[str, Counter[str]] = {}
    for record in records:
        for name, fv in record.fields.items():
            totals.setdefault(name, []).append(fv.confidence)
            types.setdefault(name, Counter())[str(fv.type)] += 1
    return {
        name: (ValueType(types[name].most_common(1)[0][0]), round(sum(scores) / len(scores), 3))
        for name, scores in sorted(totals.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
    }


def _count_types(blocks: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for block in blocks:
        obj = block["_data"]
        if isinstance(obj, dict) and block["_source"] in {"jsonld", "microdata"}:
            for name in sd.type_names(obj) or ["(untyped)"]:
                counts[name] += 1
        elif block["_source"] == "opengraph":
            counts["OpenGraph"] += 1
        elif block["_source"] == "embedded_json":
            counts[f"embedded:{block.get('_key') or 'json'}"] += 1
    return counts


def _source_of(name: str) -> Source:
    match name:
        case "jsonld":
            return Source.JSONLD
        case "microdata":
            return Source.MICRODATA
        case "opengraph":
            return Source.OPENGRAPH
        case _:
            return Source.EMBEDDED_JSON
