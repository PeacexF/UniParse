from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from lxml.html import HtmlElement

from uparse.core.models import ValueType
from uparse.extraction.htmlutil import absolutize, clean_text, node_text
from uparse.extraction.vocabulary import FIELD_TYPES, field_for_schema_key

# Schema.org types we treat as record-bearing. Anything else is kept but ranked lower.
RECORD_TYPES = frozenset(
    {
        "product",
        "offer",
        "article",
        "newsarticle",
        "blogposting",
        "jobposting",
        "event",
        "recipe",
        "review",
        "person",
        "organization",
        "localbusiness",
        "realestatelisting",
        "apartment",
        "house",
        "course",
        "book",
        "movie",
        "videoobject",
        "softwareapplication",
        "service",
        "vehicle",
        "car",
    }
)
CONTAINER_TYPES = frozenset(
    {"itemlist", "breadcrumblist", "collectionpage", "searchresultspage", "website", "webpage"}
)

_STATE_VARS = (
    "__NEXT_DATA__",
    "__NUXT__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "__APOLLO_STATE__",
    "__REDUX_STATE__",
    "__data",
    "INITIAL_STATE",
)
_ASSIGN = re.compile(
    r"(?:window\.|self\.|globalThis\.)?(" + "|".join(map(re.escape, _STATE_VARS)) + r")\s*=\s*",
)

OG_PREFIXES = ("og:", "twitter:", "product:", "article:", "book:", "music:", "video:")


def extract_all(root: HtmlElement, base_url: str) -> list[dict[str, Any]]:
    """Every structured object found on the page, tagged with its origin."""
    blocks: list[dict[str, Any]] = []
    blocks += [{"_source": "jsonld", "_data": d} for d in jsonld(root)]
    blocks += [{"_source": "microdata", "_data": d} for d in microdata(root, base_url)]
    og = opengraph(root, base_url)
    if og:
        blocks.append({"_source": "opengraph", "_data": og})
    blocks += [{"_source": "embedded_json", "_key": k, "_data": d} for k, d in embedded_json(root)]
    return blocks


# ------------------------------------------------------------------ JSON-LD


def jsonld(root: HtmlElement) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for script in root.xpath(
        '//script[@type="application/ld+json" or @type="application/ld+JSON"]'
    ):
        raw = (script.text_content() or "").strip()
        if not raw:
            continue
        for parsed in _loads_forgiving(raw):
            out.extend(flatten_jsonld(parsed))
    return out


def flatten_jsonld(node: Any) -> Iterator[dict[str, Any]]:
    """Unwrap @graph, arrays and nested node lists into a flat stream of objects."""
    if isinstance(node, list):
        for item in node:
            yield from flatten_jsonld(item)
        return
    if not isinstance(node, dict):
        return
    if "@graph" in node:
        graph = node["@graph"]
        rest = {k: v for k, v in node.items() if k != "@graph"}
        if len(rest) > 1:
            yield rest
        yield from flatten_jsonld(graph)
        return
    yield node
    for key in ("itemListElement", "mainEntity", "hasPart", "item"):
        if key in node:
            yield from flatten_jsonld(node[key])


def _loads_forgiving(raw: str) -> list[Any]:
    try:
        return [json.loads(raw)]
    except json.JSONDecodeError:
        pass
    # Some sites concatenate several objects into one script tag.
    out: list[Any] = []
    decoder = json.JSONDecoder()
    idx, n = 0, len(raw)
    while idx < n:
        while idx < n and raw[idx] in " \t\r\n,;":
            idx += 1
        if idx >= n:
            break
        try:
            value, idx = decoder.raw_decode(raw, idx)
        except json.JSONDecodeError:
            break
        out.append(value)
    return out


def type_names(obj: dict[str, Any]) -> list[str]:
    raw = obj.get("@type") or obj.get("type") or []
    values = raw if isinstance(raw, list) else [raw]
    return [str(v).rsplit("/", 1)[-1].lower() for v in values if v]


def is_record_type(obj: dict[str, Any]) -> bool:
    return any(t in RECORD_TYPES for t in type_names(obj))


# ---------------------------------------------------------------- microdata


def microdata(root: HtmlElement, base_url: str) -> list[dict[str, Any]]:
    scopes = [el for el in root.xpath("//*[@itemscope]") if _is_top_scope(el)]
    return [_microdata_item(el, base_url) for el in scopes]


def _is_top_scope(el: HtmlElement) -> bool:
    parent = el.getparent()
    while parent is not None:
        if parent.get("itemscope") is not None:
            return False
        parent = parent.getparent()
    return True


def _microdata_item(scope: HtmlElement, base_url: str) -> dict[str, Any]:
    item: dict[str, Any] = {}
    itemtype = scope.get("itemtype")
    if itemtype:
        item["@type"] = itemtype.rsplit("/", 1)[-1]
    item_id = scope.get("itemid")
    if item_id:
        item["@id"] = absolutize(base_url, item_id)
    for prop in _iter_props(scope):
        names = (prop.get("itemprop") or "").split()
        value: Any
        if prop.get("itemscope") is not None:
            value = _microdata_item(prop, base_url)
        else:
            value = _microdata_value(prop, base_url, names)
        if value in ("", None):
            continue
        for name in names:
            if name in item:
                existing = item[name]
                item[name] = [*existing, value] if isinstance(existing, list) else [existing, value]
            else:
                item[name] = value
    return item


def wants_text(names: list[str] | None) -> bool:
    """True when every property name this element carries maps to a text field."""
    if not names:
        return False
    resolved = [field_for_schema_key(name) for name in names]
    known = [FIELD_TYPES.get(field) for field in resolved if field]
    return bool(known) and all(vtype not in (ValueType.URL, None) for vtype in known)


def _iter_props(scope: HtmlElement) -> Iterator[HtmlElement]:
    for el in scope.iter():
        if el is scope or not isinstance(el.tag, str):
            continue
        if el.get("itemprop") is None:
            continue
        parent = el.getparent()
        nested = False
        while parent is not None and parent is not scope:
            if parent.get("itemscope") is not None:
                nested = True
                break
            parent = parent.getparent()
        if not nested:
            yield el


def _microdata_value(el: HtmlElement, base_url: str, names: list[str] | None = None) -> Any:
    match el.tag:
        case "meta":
            return clean_text(el.get("content"))
        case "a" | "area" | "link":
            # The spec says an anchor's value is its href, which turns
            # `<a itemprop="name">Product</a>` into a URL. Text wins for text-valued props.
            if wants_text(names):
                text = node_text(el, limit=2000)
                if text:
                    return text
            return absolutize(base_url, el.get("href"))
        case "img" | "audio" | "video" | "embed" | "iframe" | "source" | "track":
            return absolutize(base_url, el.get("src"))
        case "object":
            return absolutize(base_url, el.get("data"))
        case "time":
            return clean_text(el.get("datetime")) or node_text(el)
        case "data" | "meter":
            return clean_text(el.get("value")) or node_text(el)
        case _:
            return clean_text(el.get("content")) or node_text(el, limit=2000)


# ---------------------------------------------------------------- opengraph


def opengraph(root: HtmlElement, base_url: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for meta in root.iter("meta"):
        key = meta.get("property") or meta.get("name") or ""
        key = key.strip().lower()
        if not key.startswith(OG_PREFIXES):
            continue
        content = clean_text(meta.get("content"))
        if not content:
            continue
        short = key.split(":", 1)[1] if ":" in key else key
        if short in {"image", "url", "video", "audio", "secure_url"}:
            content = absolutize(base_url, content) or content
        out.setdefault(short, content)
    canonical = root.xpath('//link[@rel="canonical"]/@href')
    if canonical:
        out.setdefault("canonical", absolutize(base_url, str(canonical[0])))
    description = root.xpath('//meta[@name="description"]/@content')
    if description:
        out.setdefault("description", clean_text(str(description[0])))
    return out


# ----------------------------------------------------------- embedded state


def embedded_json(root: HtmlElement, *, max_bytes: int = 4_000_000) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for script in root.iter("script"):
        stype = (script.get("type") or "").lower()
        if "ld+json" in stype:
            continue  # already handled by jsonld()
        if stype and "json" not in stype and "javascript" not in stype and stype != "module":
            continue
        raw = script.text_content() or ""
        if not raw or len(raw) > max_bytes:
            continue
        script_id = script.get("id") or ""
        if "json" in stype or script_id in _STATE_VARS:
            parsed = _try_json(raw.strip())
            if parsed is not None:
                out.append((script_id or stype, parsed))
                continue
        for key, value in _scan_assignments(raw):
            out.append((key, value))
    return out


def _scan_assignments(raw: str) -> Iterator[tuple[str, Any]]:
    decoder = json.JSONDecoder()
    for match in _ASSIGN.finditer(raw):
        start = match.end()
        while start < len(raw) and raw[start] in " \t\r\n":
            start += 1
        if start >= len(raw) or raw[start] not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            continue
        yield match.group(1), value


def _try_json(raw: str) -> Any:
    if not raw or raw[0] not in "{[":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def iter_objects(
    data: Any, *, max_depth: int = 8, max_nodes: int = 20_000
) -> Iterator[dict[str, Any]]:
    """Depth-bounded walk over embedded state looking for record-shaped dicts."""
    stack: list[tuple[Any, int]] = [(data, 0)]
    seen = 0
    while stack and seen < max_nodes:
        node, depth = stack.pop()
        seen += 1
        if depth > max_depth:
            continue
        if isinstance(node, dict):
            yield node
            stack.extend((v, depth + 1) for v in node.values() if isinstance(v, dict | list))
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node if isinstance(v, dict | list))
