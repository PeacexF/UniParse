from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field

from lxml.html import HtmlElement

from uparse.extraction.htmlutil import node_text

SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "path", "head", "meta", "link", "br", "hr"}
)
# Class-name noise that carries no structural meaning (utility/CSS-module suffixes).
_HASHY = re.compile(
    r"^(?:[a-z]+[-_])?[a-f0-9]{5,}$|^[\w-]*__[a-zA-Z0-9]{4,}$|^(?:css|sc|jsx)-\w+$", re.I
)
_DIGITS = re.compile(r"\d+")


@dataclass(slots=True)
class Fingerprint:
    tag: str
    depth: int
    parent_tag: str
    child_tags: tuple[str, ...]
    class_tokens: frozenset[str]
    attr_names: frozenset[str]
    text_len: int
    link_count: int
    image_count: int
    descendant_count: int
    heading_count: int
    digest: str = field(default="", compare=False)

    def key(self) -> str:
        return self.digest


def stable_classes(node: HtmlElement) -> frozenset[str]:
    raw = (node.get("class") or "").split()
    keep = {
        _DIGITS.sub("#", token.lower())
        for token in raw
        if 1 < len(token) <= 40 and not _HASHY.match(token)
    }
    return frozenset(keep)


def fingerprint(node: HtmlElement, depth: int = 0) -> Fingerprint:
    child_tags: list[str] = []
    descendants = 0
    links = images = headings = 0
    for el in node.iter():
        if el is node or not isinstance(el.tag, str) or el.tag in SKIP_TAGS:
            continue
        descendants += 1
        if el.tag == "a":
            links += 1
        elif el.tag in {"img", "picture", "source"}:
            images += 1
        elif el.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            headings += 1
    for child in node:
        if isinstance(child.tag, str) and child.tag not in SKIP_TAGS:
            child_tags.append(child.tag)

    parent = node.getparent()
    fp = Fingerprint(
        tag=node.tag,
        depth=depth,
        parent_tag=parent.tag if parent is not None and isinstance(parent.tag, str) else "",
        child_tags=tuple(child_tags[:24]),
        class_tokens=stable_classes(node),
        attr_names=frozenset(k for k in node.attrib if not k.startswith("data-v-")),
        text_len=len(node_text(node, limit=4000)),
        link_count=links,
        image_count=images,
        descendant_count=descendants,
        heading_count=headings,
    )
    fp.digest = _digest(fp)
    return fp


def _digest(fp: Fingerprint) -> str:
    blob = "|".join(
        [
            fp.tag,
            ",".join(sorted(fp.class_tokens)),
            ",".join(Counter(fp.child_tags).elements()),
            ",".join(sorted(fp.attr_names & {"href", "src", "itemprop", "role", "id"})),
        ]
    )
    return hashlib.blake2b(blob.encode(), digest_size=8).hexdigest()


def similarity(a: Fingerprint, b: Fingerprint) -> float:
    """0..1 structural similarity. Deterministic and cheap; no ML anywhere near it."""
    if a.tag != b.tag:
        return 0.0
    score = 0.0
    score += 0.20  # same tag
    score += 0.30 * _jaccard(a.class_tokens, b.class_tokens, empty=0.5)
    score += 0.20 * _multiset_overlap(a.child_tags, b.child_tags)
    score += 0.10 * _jaccard(a.attr_names, b.attr_names, empty=0.5)
    score += 0.10 * _ratio(a.descendant_count, b.descendant_count)
    score += 0.05 * _ratio(a.text_len, b.text_len)
    score += 0.05 * (1.0 if (a.link_count > 0) == (b.link_count > 0) else 0.0)
    return round(min(score, 1.0), 4)


def _jaccard(a: frozenset[str], b: frozenset[str], *, empty: float = 0.0) -> float:
    if not a and not b:
        return empty
    union = a | b
    return len(a & b) / len(union) if union else empty


def _multiset_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    if not a and not b:
        return 1.0
    ca, cb = Counter(a), Counter(b)
    inter = sum((ca & cb).values())
    total = max(sum(ca.values()), sum(cb.values()))
    return inter / total if total else 0.0


def _ratio(a: int, b: int) -> float:
    hi = max(a, b)
    return 1.0 if hi == 0 else min(a, b) / hi


def is_container(node: HtmlElement) -> bool:
    return isinstance(node.tag, str) and node.tag not in SKIP_TAGS


def text_density(node: HtmlElement) -> float:
    html_len = len(node_text(node, limit=8000)) or 1
    return html_len / max(len(node.getchildren()) or 1, 1)
