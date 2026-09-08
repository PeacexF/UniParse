from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import urldefrag, urljoin

from lxml import etree
from lxml import html as lxml_html
from lxml.html import HtmlElement

from uparse.core.models import Image, Link, PageModel

_WS = re.compile(r"\s+")
_INVISIBLE = frozenset({"script", "style", "noscript", "template", "svg", "head", "meta", "link"})

_parser = lxml_html.HTMLParser(encoding="utf-8", recover=True, remove_comments=True)


def parse(html: str, base_url: str = "") -> HtmlElement:
    if not html.strip():
        html = "<html><body></body></html>"
    root = cast(
        HtmlElement, lxml_html.document_fromstring(html.encode("utf-8", "replace"), parser=_parser)
    )
    if base_url:
        root.make_links_absolute(base_url, resolve_base_href=True, handle_failures="ignore")
    return root


def clean_text(value: str | None) -> str:
    return "" if not value else _WS.sub(" ", value).strip()


def node_text(node: HtmlElement, *, limit: int | None = None) -> str:
    parts: list[str] = []
    total = 0
    for chunk in node.itertext():
        parts.append(chunk)
        total += len(chunk)
        if limit is not None and total > limit:
            break
    return clean_text("".join(parts))


def visible_text(root: HtmlElement) -> str:
    parts: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str) or el.tag in _INVISIBLE:
            continue
        if el.text:
            parts.append(el.text)
        if el.tail:
            parts.append(el.tail)
    return clean_text(" ".join(parts))


def title_of(root: HtmlElement) -> str | None:
    for xp in ("//title/text()", "//meta[@property='og:title']/@content", "//h1//text()"):
        found = root.xpath(xp)
        if found:
            text = clean_text(str(found[0]))
            if text:
                return text
    return None


def absolutize(base_url: str, value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if not value or value.startswith(("javascript:", "mailto:", "tel:", "#")):
        return ""
    try:
        return urldefrag(urljoin(base_url, value)).url
    except ValueError:
        return ""


def iter_links(root: HtmlElement, base_url: str) -> Iterator[Link]:
    for a in root.iter("a"):
        url = absolutize(base_url, a.get("href"))
        if not url:
            continue
        yield Link(
            url=url,
            text=node_text(a, limit=300),
            rel=clean_text(a.get("rel") or ""),
            title=clean_text(a.get("title") or ""),
        )


def image_url(node: HtmlElement, base_url: str) -> str:
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        url = absolutize(base_url, node.get(attr))
        if url and not url.startswith("data:"):
            return url
    srcset = node.get("srcset") or node.get("data-srcset")
    if srcset:
        best = _best_from_srcset(srcset)
        if best:
            return absolutize(base_url, best)
    return ""


def _best_from_srcset(srcset: str) -> str:
    best_url, best_w = "", -1.0
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        url = bits[0]
        width = -1.0
        if len(bits) > 1 and bits[1][:-1].replace(".", "", 1).isdigit():
            width = float(bits[1][:-1])
        if width > best_w:
            best_url, best_w = url, width
    return best_url


def iter_images(root: HtmlElement, base_url: str) -> Iterator[Image]:
    for img in root.iter("img"):
        url = image_url(img, base_url)
        if not url:
            continue
        yield Image(
            url=url,
            alt=clean_text(img.get("alt") or ""),
            title=clean_text(img.get("title") or ""),
            width=_int(img.get("width")),
            height=_int(img.get("height")),
        )


def _int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def css_path(node: HtmlElement, root: HtmlElement | None = None) -> str:
    """Short, human-readable selector used for provenance, not for re-querying."""
    parts: list[str] = []
    current: HtmlElement | None = node
    while current is not None and isinstance(current.tag, str):
        if current is root or current.tag in {"html", "body"}:
            break
        step = current.tag
        node_id = current.get("id")
        if node_id and " " not in node_id:
            parts.append(f"#{node_id}")
            break
        classes = [c for c in (current.get("class") or "").split() if c and "  " not in c][:2]
        if classes:
            step += "." + ".".join(classes)
        else:
            parent = current.getparent()
            siblings = [] if parent is None else [s for s in parent if s.tag == current.tag]
            if len(siblings) > 1:
                step += f":nth-of-type({siblings.index(current) + 1})"
        parts.append(step)
        current = current.getparent()
    return " > ".join(reversed(parts)) or node.tag


def xpath_of(node: HtmlElement) -> str:
    tree = node.getroottree()
    return str(tree.getpath(node))


def select(root: HtmlElement, selector: str) -> list[HtmlElement]:
    try:
        return [el for el in root.cssselect(selector) if isinstance(el.tag, str)]
    except Exception:  # invalid selector, unsupported pseudo-class
        try:
            found = root.xpath(selector)
        except etree.XPathError, TypeError:
            return []
        return [el for el in found if isinstance(el, HtmlElement)]


def hydrate(page: PageModel, root: HtmlElement | None = None) -> HtmlElement:
    """Fill the derived parts of a PageModel from its HTML. Idempotent-ish and cheap."""
    tree = root if root is not None else parse(page.html, page.base_url)
    if not page.title:
        page.title = title_of(tree)
    if not page.text:
        page.text = visible_text(tree)
    if not page.links:
        page.links = list(iter_links(tree, page.base_url))
    if not page.images:
        page.images = list(iter_images(tree, page.base_url))
    return tree


def attrs_of(node: HtmlElement) -> dict[str, Any]:
    return dict(node.attrib)
