from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from lxml.html import HtmlElement

from uparse.config.schema import PaginationConfig
from uparse.core.models import PageModel, PaginationHint
from uparse.extraction.htmlutil import absolutize, node_text, select

# Deliberately multilingual: freelance jobs are rarely English-only.
NEXT_TEXT = frozenset(
    {
        "next",
        "next page",
        "next »",
        "older",
        "older posts",
        "older entries",
        "weiter",
        "nächste",
        "nächste seite",
        "suivant",
        "suivante",
        "page suivante",
        "siguiente",
        "próxima",
        "proxima",
        "seguinte",
        "avanti",
        "successiva",
        "volgende",
        "næste",
        "nästa",
        "seuraava",
        "następna",
        "další",
        "далее",
        "следующая",
        "вперёд",
        "вперед",
        "keyingi",
        "次へ",
        "次のページ",
        "下一页",
        "下一頁",
        "다음",
    }
)
NEXT_GLYPHS = frozenset({"›", "»", "→", ">", ">>", "▶", "❯", "⟩"})
PREV_TEXT = frozenset(
    {"prev", "previous", "back", "zurück", "précédent", "anterior", "назад", "предыдущая"}
)

_NEXT_CLASS = re.compile(r"(?:^|[-_ ])(next|forward|fwd|weiter|suivant|siguiente)(?:$|[-_ ])", re.I)
_PAGE_PARAMS = ("page", "p", "pg", "pagina", "seite", "offset", "start", "from", "skip")
_PATH_PAGE = re.compile(r"(/(?:page|seite|pagina|p)/)(\d+)(/?)$", re.I)


def find_next(
    root: HtmlElement, page: PageModel, config: PaginationConfig, *, seen: set[str] | None = None
) -> PaginationHint | None:
    """Best next-page URL for this page, or None when the run should stop here."""
    if not config.enabled:
        return None
    visited = seen or set()
    for hint in _candidates(root, page, config):
        if hint.url and hint.url not in visited and hint.url != page.base_url:
            return hint
    return None


def _candidates(
    root: HtmlElement, page: PageModel, config: PaginationConfig
) -> list[PaginationHint]:
    out: list[PaginationHint] = []
    base = page.base_url

    if config.selector:
        for node in select(root, config.selector):
            url = absolutize(base, node.get("href"))
            if url:
                out.append(
                    PaginationHint(url, "configured", 1.0, config.selector, "explicit selector")
                )
                break

    if config.url_template:
        current = _current_page_number(base) or 1
        url = config.url_template.replace("{page}", str(current + 1))
        out.append(PaginationHint(url, "template", 1.0, None, config.url_template))

    out += _rel_next(root, base)
    out += _link_text(root, base)
    out += _numeric(root, base)
    out += _url_increment(root, base)
    out.sort(key=lambda h: -h.confidence)
    return out


def _rel_next(root: HtmlElement, base: str) -> list[PaginationHint]:
    out: list[PaginationHint] = []
    for node in root.xpath("//link[@rel] | //a[@rel]"):
        rels = (node.get("rel") or "").lower().split()
        if "next" not in rels:
            continue
        url = absolutize(base, node.get("href"))
        if url:
            out.append(
                PaginationHint(url, "rel=next", 0.95, "[rel=next]", f"<{node.tag} rel=next>")
            )
    return out


def _link_text(root: HtmlElement, base: str) -> list[PaginationHint]:
    out: list[PaginationHint] = []
    for a in root.iter("a"):
        url = absolutize(base, a.get("href"))
        if not url:
            continue
        text = node_text(a, limit=60).lower().strip(" .:")
        label = (a.get("aria-label") or a.get("title") or "").lower().strip()
        classes = a.get("class") or ""

        if text in PREV_TEXT or label in PREV_TEXT:
            continue
        if text in NEXT_TEXT or label in NEXT_TEXT:
            out.append(PaginationHint(url, "link text", 0.8, None, f"link text {text or label!r}"))
        elif text in NEXT_GLYPHS or (
            any(text.endswith(g) for g in NEXT_GLYPHS) and len(text) <= 12
        ):
            confidence = 0.7 if _NEXT_CLASS.search(classes) else 0.55
            out.append(PaginationHint(url, "link glyph", confidence, None, f"glyph {text!r}"))
        elif _NEXT_CLASS.search(classes):
            out.append(
                PaginationHint(url, "class name", 0.6, f".{classes.split()[0]}", "next-like class")
            )
    return out


def _numeric(root: HtmlElement, base: str) -> list[PaginationHint]:
    """Follow the numbered pager: find the current page, take the link labelled n+1."""
    current = _current_page_number(base)
    numbered: dict[int, str] = {}
    for a in root.iter("a"):
        text = node_text(a, limit=12).strip()
        if not text.isdigit():
            continue
        url = absolutize(base, a.get("href"))
        if url:
            numbered.setdefault(int(text), url)
    if not numbered:
        return []
    if current is None:
        current = _marked_current(root) or min(numbered) - 1
    target = numbered.get(current + 1)
    return (
        [PaginationHint(target, "numbered pager", 0.65, None, f"page {current + 1}")]
        if target
        else []
    )


def _marked_current(root: HtmlElement) -> int | None:
    for node in root.xpath(
        "//*[@aria-current] | //*[contains(@class,'current') or contains(@class,'active')]"
    ):
        text = node_text(node, limit=12).strip()
        if text.isdigit():
            return int(text)
    return None


def _url_increment(root: HtmlElement, base: str) -> list[PaginationHint]:
    """Last resort: bump a page parameter, but only when the page looks paginated."""
    pagers = _pagers(root)
    if not pagers:
        return []
    if _looks_like_last_page(pagers, _current_page_number(base)):
        return []
    bumped = next_url_by_pattern(base)
    return (
        [PaginationHint(bumped, "url pattern", 0.4, None, "incremented page parameter")]
        if bumped
        else []
    )


def _pagers(root: HtmlElement) -> list[HtmlElement]:
    return list(
        root.xpath(
            "//*[contains(@class,'pagination') or contains(@class,'pager')"
            " or contains(@class,'paging') or @role='navigation']"
        )
    )


def _pager_numbers(pager: HtmlElement) -> list[int]:
    return [int(t) for a in pager.iter("a") if (t := node_text(a, limit=12).strip()).isdigit()]


def _looks_like_last_page(pagers: list[HtmlElement], current: int | None) -> bool:
    """A pager that only points backwards means there is nothing left to fetch."""
    numbers = [n for pager in pagers for n in _pager_numbers(pager)]
    if numbers:
        return current is not None and max(numbers) <= current
    anchors = [a for pager in pagers for a in pager.iter("a")]
    labels = [
        (node_text(a, limit=60).lower().strip(" .:"), (a.get("aria-label") or "").lower())
        for a in anchors
    ]
    has_prev = any(text in PREV_TEXT or label in PREV_TEXT for text, label in labels)
    has_next = any(
        text in NEXT_TEXT
        or label in NEXT_TEXT
        or text in NEXT_GLYPHS
        or _NEXT_CLASS.search(a.get("class") or "")
        for (text, label), a in zip(labels, anchors, strict=True)
    )
    return has_prev and not has_next


def next_url_by_pattern(url: str) -> str | None:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in _PAGE_PARAMS:
        raw = query.get(key)
        if raw is None or not raw.isdigit():
            continue
        step = _step_for(key, int(raw))
        query[key] = str(int(raw) + step)
        return urlunparse(parsed._replace(query=urlencode(query)))
    match = _PATH_PAGE.search(parsed.path)
    if match:
        path = _PATH_PAGE.sub(rf"\g<1>{int(match.group(2)) + 1}\g<3>", parsed.path)
        return urlunparse(parsed._replace(path=path))
    return None


def _step_for(key: str, value: int) -> int:
    # offset-style parameters advance by a page size, not by one.
    return value if key in {"offset", "start", "skip", "from"} and value > 0 else 1


def _current_page_number(url: str) -> int | None:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    for key in ("page", "p", "pg", "pagina", "seite"):
        raw = query.get(key)
        if raw and raw.isdigit():
            return int(raw)
    match = _PATH_PAGE.search(parsed.path)
    return int(match.group(2)) if match else None
