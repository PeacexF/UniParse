from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from lxml.html import HtmlElement

from uparse.core.models import CollectionCandidate, Signal
from uparse.extraction.dom import SKIP_TAGS, Fingerprint, fingerprint, similarity, stable_classes
from uparse.extraction.htmlutil import css_path, node_text

# Tags that almost never wrap a record on their own.
_WEAK_TAGS = frozenset({"span", "b", "i", "em", "strong", "br", "label", "option", "th", "td"})
_STRONG_TAGS = frozenset({"article", "li", "tr"})
_RECORDISH_CLASSES = frozenset(
    {
        "item",
        "items",
        "card",
        "cards",
        "product",
        "products",
        "listing",
        "listings",
        "entry",
        "entries",
        "result",
        "results",
        "row",
        "rows",
        "post",
        "posts",
        "tile",
        "tiles",
        "cell",
        "teaser",
        "article",
        "job",
        "offer",
        "ad",
        "grid-item",
    }
)
_NAV_CLASSES = frozenset(
    {
        "nav",
        "navbar",
        "menu",
        "breadcrumb",
        "breadcrumbs",
        "pagination",
        "pager",
        "footer",
        "header",
        "sidebar",
        "social",
        "tabs",
    }
)

MIN_GROUP = 2
MIN_TEXT_PER_ITEM = 8


@dataclass(slots=True)
class _Group:
    parent: HtmlElement
    nodes: list[HtmlElement]
    fingerprints: list[Fingerprint]


def discover(
    root: HtmlElement, *, min_records: int = 2, limit: int = 12
) -> list[CollectionCandidate]:
    """Rank repeated sibling structures that look like lists of records."""
    candidates = [_score(group, root) for group in _groups(root, min_records)]
    candidates = [c for c in candidates if c.confidence > 0.15]
    candidates.sort(key=lambda c: (-c.confidence, -c.count))
    return _drop_nested(candidates)[:limit]


def _groups(root: HtmlElement, min_records: int) -> list[_Group]:
    out: list[_Group] = []
    for parent in root.iter():
        if not isinstance(parent.tag, str) or parent.tag in SKIP_TAGS:
            continue
        children = [c for c in parent if isinstance(c.tag, str) and c.tag not in SKIP_TAGS]
        if len(children) < max(min_records, MIN_GROUP):
            continue
        buckets: dict[str, list[HtmlElement]] = defaultdict(list)
        prints: dict[int, Fingerprint] = {}
        for child in children:
            fp = fingerprint(child)
            prints[id(child)] = fp
            buckets[f"{child.tag}:{','.join(sorted(fp.class_tokens))}"].append(child)
        for nodes in buckets.values():
            if len(nodes) < max(min_records, MIN_GROUP):
                continue
            out.append(_Group(parent, nodes, [prints[id(n)] for n in nodes]))
    return out


def _score(group: _Group, root: HtmlElement) -> CollectionCandidate:
    nodes, prints = group.nodes, group.fingerprints
    tag = nodes[0].tag
    signals: list[Signal] = []

    base = 0.15 + min(len(nodes), 20) / 20 * 0.20
    signals.append(Signal("repeated siblings", round(base, 3), f"{len(nodes)}× <{tag}>"))
    score = base

    cohesion = _cohesion(prints)
    weight = 0.30 * cohesion
    signals.append(Signal("structural similarity", round(weight, 3), f"cohesion {cohesion:.2f}"))
    score += weight

    if tag in _STRONG_TAGS:
        signals.append(Signal("record-shaped tag", 0.12, f"<{tag}>"))
        score += 0.12
    elif tag in _WEAK_TAGS:
        signals.append(Signal("weak tag", -0.15, f"<{tag}>"))
        score -= 0.15

    classes = set().union(*(fp.class_tokens for fp in prints)) if prints else set()
    parent_classes = stable_classes(group.parent)
    if _matches(classes, _RECORDISH_CLASSES) or _matches(parent_classes, _RECORDISH_CLASSES):
        signals.append(Signal("record-like class", 0.12, "item/card/product/result token"))
        score += 0.12
    if _matches(classes | parent_classes, _NAV_CLASSES):
        signals.append(Signal("navigation-like class", -0.30, "nav/menu/pagination token"))
        score -= 0.30

    avg_text = sum(fp.text_len for fp in prints) / len(prints)
    if avg_text < MIN_TEXT_PER_ITEM:
        signals.append(Signal("too little text", -0.25, f"{avg_text:.0f} chars/item"))
        score -= 0.25
    elif avg_text > 40:
        signals.append(Signal("substantial text", 0.08, f"{avg_text:.0f} chars/item"))
        score += 0.08

    if all(fp.link_count > 0 for fp in prints):
        signals.append(Signal("every item links out", 0.10, ""))
        score += 0.10
    if sum(fp.image_count for fp in prints) >= len(prints):
        signals.append(Signal("every item has an image", 0.06, ""))
        score += 0.06
    if any(fp.heading_count > 0 for fp in prints):
        signals.append(Signal("headings inside items", 0.06, ""))
        score += 0.06
    if all(fp.descendant_count <= 1 for fp in prints):
        signals.append(Signal("items are leaves", -0.20, "no internal structure"))
        score -= 0.20

    label = _label(nodes[0], classes)
    return CollectionCandidate(
        selector=css_path(nodes[0], root),
        container_selector=css_path(group.parent, root),
        count=len(nodes),
        confidence=round(max(0.0, min(score, 1.0)), 4),
        label=label,
        signals=signals,
        fingerprint=prints[0].digest,
        nodes=nodes,
    )


def _cohesion(prints: list[Fingerprint]) -> float:
    if len(prints) < 2:
        return 0.0
    reference = prints[0]
    sample = prints[1:16]
    return sum(similarity(reference, fp) for fp in sample) / len(sample)


def _matches(tokens: set[str] | frozenset[str], vocabulary: frozenset[str]) -> bool:
    return any(
        part in vocabulary for token in tokens for part in token.replace("_", "-").split("-")
    )


def _label(node: HtmlElement, classes: set[str]) -> str:
    for token in sorted(classes, key=len):
        for part in token.replace("_", "-").split("-"):
            if part in _RECORDISH_CLASSES:
                return part
    itemtype = node.get("itemtype")
    if itemtype:
        return str(itemtype).rsplit("/", 1)[-1].lower()
    return str(node.tag)


def _drop_nested(candidates: list[CollectionCandidate]) -> list[CollectionCandidate]:
    """Keep the outermost winner when one group's items contain another's."""
    kept: list[CollectionCandidate] = []
    for cand in candidates:
        owned = {id(n) for n in cand.nodes}
        if any(
            _is_descendant(cand.nodes[0], k.nodes) or id(cand.nodes[0]) in {id(x) for x in k.nodes}
            for k in kept
        ):
            continue
        del owned
        kept.append(cand)
    return kept


def _is_descendant(node: HtmlElement, ancestors: list[HtmlElement]) -> bool:
    ids = {id(a) for a in ancestors}
    parent = node.getparent()
    while parent is not None:
        if id(parent) in ids:
            return True
        parent = parent.getparent()
    return False


def item_texts(candidate: CollectionCandidate, limit: int = 3) -> list[str]:
    return [str(node_text(n, limit=200)) for n in candidate.nodes[:limit]]
