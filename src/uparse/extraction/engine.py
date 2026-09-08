from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from lxml.etree import tostring
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
from uparse.extraction import alignment as align
from uparse.extraction import collections as coll
from uparse.extraction import fields as fld
from uparse.extraction import structured as sd
from uparse.extraction import tables as tbl
from uparse.extraction.htmlutil import (
    absolutize,
    hydrate,
    image_url,
    node_text,
    parse,
    select,
    select_within,
)
from uparse.extraction.vocabulary import FIELD_TYPES
from uparse.processing.normalize import coerce, normalize_enum
from uparse.processing.scoring import (
    SOURCE_BASE,
    apply_consistency,
    base_signal,
    best_per_field,
    score,
)

Strategy = str

# Structured data is machine-readable and authored by the site; only a configured
# collection (confidence 1.0) outranks it.
STRUCTURED_QUALITY = 0.95
# A whole page has far more nodes than one record, so the per-record cap is lifted here.
PAGE_NODE_LIMIT = 4_000


@dataclass(slots=True)
class _Attempt:
    strategy: Strategy
    records: list[Record]
    quality: float
    collection: CollectionCandidate | None = None


@dataclass(slots=True)
class ExtractionResult:
    records: list[Record] = dc_field(default_factory=list)
    strategy: Strategy = "none"
    quality: float = 0.0
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

    attempt = _arbitrate(root, blocks, page, cfg, result)
    records = attempt.records
    result.strategy = attempt.strategy
    result.quality = attempt.quality
    result.collection = attempt.collection

    _apply_config_fields(records, root, page, cfg)
    _finalize(records, page, cfg)

    if cfg.min_confidence > 0:
        records = [r for r in records if r.confidence >= cfg.min_confidence]
    result.records = records[: cfg.max_records_per_page]
    result.schema = _schema(result.records)
    return result


def _arbitrate(
    root: HtmlElement,
    blocks: list[dict[str, Any]],
    page: PageModel,
    cfg: ExtractionConfig,
    result: ExtractionResult,
) -> _Attempt:
    """Run every viable strategy and keep the best, rather than the first that answers.

    A layout table used to pre-empt a high-confidence DOM collection simply because
    tables were tried first; now each strategy states a quality and the highest wins.
    """
    attempts: list[_Attempt] = []

    structured = _from_structured(blocks, page, cfg)
    if structured:
        attempts.append(_Attempt("structured", structured, STRUCTURED_QUALITY))

    if cfg.include_tables:
        table = tbl.best(root, min_rows=cfg.min_records)
        if table is not None:
            rows = tbl.rows_of(table, page.base_url, root)
            if rows:
                attempts.append(_Attempt("table", rows, table.confidence))
                result.notes.append(f"table candidate: {table.confidence:.2f} ({table.rows} rows)")

    dom_records, chosen = _from_dom(root, page, cfg, result.collections)
    if dom_records and chosen is not None:
        attempts.append(_Attempt("dom", dom_records, chosen.confidence, chosen))

    if attempts:
        best = max(attempts, key=lambda a: (a.quality, len(a.records)))
        if len(attempts) > 1:
            losers = ", ".join(f"{a.strategy} {a.quality:.2f}" for a in attempts if a is not best)
            result.notes.append(f"chose {best.strategy} {best.quality:.2f} over {losers}")
        return best

    single = _single_record(blocks, page, cfg)
    return _Attempt("page" if single else "none", single, 0.3 if single else 0.0)


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

    per_record = [fld.from_node(node, page.base_url) for node in chosen.nodes]
    # Columns the whole collection shares fill the gaps the vocabulary cannot name.
    for found, aligned in zip(
        per_record, align.candidates(chosen.nodes, page.base_url), strict=True
    ):
        found += aligned
    grouped = [fld.group_by_field(c) for c in per_record]
    apply_consistency(grouped)
    records = []
    for index, (cands, node) in enumerate(zip(per_record, chosen.nodes, strict=True)):
        record = _record_from_candidates(cands, index, page, chosen.label)
        record.node = node
        records.append(record)
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


def extract_one(page: PageModel, config: ExtractionConfig | None = None) -> Record | None:
    """Everything the page says about the single thing it is about.

    Detail pages are not collections: a product page's specification table is a list of
    that product's properties, not a list of products, so collection and table strategies
    are skipped entirely here.
    """
    cfg = config or ExtractionConfig()
    root = parse(page.html, page.base_url)
    hydrate(page, root)
    blocks = sd.extract_all(root, page.base_url) if cfg.include_structured_data else []

    records = _single_record(blocks, page, cfg)
    record = records[0] if records else Record(index=0, page_url=page.base_url, collection="page")
    record.node = root

    if cfg.include_tables:
        for name, value in tbl.property_pairs(root).items():
            if name not in record.fields:
                target = FIELD_TYPES.get(name, ValueType.STRING)
                coerced, actual = coerce(value, target, base_url=page.base_url)
                if coerced not in (None, ""):
                    record.set(
                        FieldValue(
                            name=name,
                            value=coerced,
                            raw=value,
                            type=actual,
                            confidence=SOURCE_BASE[Source.TABLE],
                            source=Source.TABLE,
                            selector=f"table tr:contains({name})",
                            signals=[base_signal(Source.TABLE, "property table")],
                        )
                    )

    # The page's own markup, not just its structured data: a detail page names most of
    # what it knows in classes and headings exactly as a listing card does.
    scope = _main_content(root)
    for name, cand in best_per_field(
        fld.from_node(scope, page.base_url, limit=PAGE_NODE_LIMIT)
    ).items():
        if name not in record.fields:
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

    _apply_config_fields([record], root, page, cfg)
    _finalize([record], page, cfg)
    return record if record.fields else None


def _main_content(root: HtmlElement) -> HtmlElement:
    """Narrow to the part of the page that is about the thing, skipping nav and footer."""
    for selector in ("main", "article", "[role=main]", "#content", "#main"):
        found = select(root, selector)
        if found:
            return found[0]
    body = root.find("body")
    return body if body is not None else root


# --------------------------------------------------------- config overrides


def _apply_config_fields(
    records: list[Record], root: HtmlElement, page: PageModel, cfg: ExtractionConfig
) -> None:
    """Explicit configuration always wins; it runs last and overwrites inference.

    A selector is resolved inside the record's own node whenever the record has one, so
    a pinned field varies per record instead of repeating the first match on the page.
    """
    if not cfg.fields:
        return
    for name, spec in cfg.fields.items():
        if spec.mode == "auto":
            continue
        for record in records:
            scope = record.node if record.node is not None else root
            value = _apply_spec(spec, scope, page, name)
            if value is None and spec.default is not None:
                value = spec.default
            if value is None:
                record.fields.pop(name, None)
                continue
            vtype = _config_type(spec, name)
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


def _apply_spec(spec: FieldSpec, scope: HtmlElement, page: PageModel, name: str = "") -> Any:
    match spec.mode:
        case "constant":
            return spec.value
        case "regex":
            haystack = (
                node_text(scope) if scope.getparent() is not None else (page.text or page.html)
            )
            found = re.search(spec.regex or "", haystack)
            return (found.group(1) if found.groups() else found.group(0)) if found else None
        case "attribute":
            found_nodes = select_within(scope, spec.selector or "")
            if not found_nodes:
                return None
            if spec.many:
                return [_attr_value(n, spec.attribute or "", page) for n in found_nodes]
            return _attr_value(found_nodes[0], spec.attribute or "", page)
        case "selector":
            found_nodes = select_within(scope, spec.selector or "")
            if not found_nodes:
                return None
            wants_url = _wants_url(spec, name)
            if spec.many:
                return [_node_value(n, page, wants_url=wants_url) for n in found_nodes]
            return _node_value(found_nodes[0], page, wants_url=wants_url)
        case _:
            return None


def _config_type(spec: FieldSpec, name: str) -> ValueType:
    """A declared type wins; asking for `text` means text, whatever the field is called."""
    if spec.type != "auto":
        return ValueType(spec.type)
    if spec.mode == "attribute" and spec.attribute in {"text", "html"}:
        return ValueType.STRING
    return FIELD_TYPES.get(name, ValueType.STRING)


def _wants_url(spec: FieldSpec, name: str) -> bool:
    """A bare selector yields text unless the field is a URL — `attribute` overrides both."""
    declared = ValueType(spec.type) if spec.type != "auto" else FIELD_TYPES.get(name)
    return declared is ValueType.URL


def _node_value(node: HtmlElement, page: PageModel, *, wants_url: bool) -> Any:
    if wants_url:
        if node.tag == "img":
            return image_url(node, page.base_url)
        href = node.get("href") or node.get("src") or node.get("content")
        if href:
            return absolutize(page.base_url, href)
    return node_text(node, limit=4000)


def _attr_value(node: HtmlElement, attribute: str, page: PageModel) -> Any:
    """`text` and `html` are pseudo-attributes: the escape hatch for "text, not href"."""
    match attribute:
        case "text":
            return node_text(node, limit=4000)
        case "html":
            return tostring(node, encoding="unicode", with_tail=False)
        case "href" | "src" | "srcset" | "data-src":
            return absolutize(page.base_url, node.get(attribute)) or node.get(attribute)
        case _:
            return node.get(attribute)


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
