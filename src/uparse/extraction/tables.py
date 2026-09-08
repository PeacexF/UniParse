from __future__ import annotations

from lxml.html import HtmlElement

from uparse.core.models import FieldValue, Record, Signal, Source
from uparse.extraction.htmlutil import absolutize, css_path, image_url, node_text
from uparse.extraction.vocabulary import field_for_token, tokens_of
from uparse.processing.normalize import infer_type
from uparse.processing.scoring import BONUS, base_signal

MAX_COLUMNS = 60


def extract(root: HtmlElement, base_url: str, *, min_rows: int = 2) -> list[Record]:
    best = _best_table(root, min_rows)
    if best is None:
        return []
    return _rows(best, base_url, root)


def _best_table(root: HtmlElement, min_rows: int) -> HtmlElement | None:
    best: HtmlElement | None = None
    best_cells = 0
    for table in root.iter("table"):
        rows = table.findall(".//tr")
        if len(rows) < min_rows + 1:
            continue
        # Layout tables: one cell per row carries no tabular meaning.
        widths = [len(r.findall("./td")) + len(r.findall("./th")) for r in rows]
        if max(widths, default=0) < 2:
            continue
        cells = sum(widths)
        if cells > best_cells:
            best, best_cells = table, cells
    return best


def _headers(table: HtmlElement) -> list[str]:
    head_row = table.find(".//thead//tr")
    if head_row is None:
        first = table.find(".//tr")
        if first is not None and first.findall("./th"):
            head_row = first
    if head_row is None:
        return []
    return [node_text(c, limit=200) for c in head_row.findall("./th") + head_row.findall("./td")]


def _rows(table: HtmlElement, base_url: str, root: HtmlElement) -> list[Record]:
    headers = _headers(table)
    names = _column_names(headers)
    records: list[Record] = []
    index = 0
    for tr in table.findall(".//tr"):
        cells = tr.findall("./td")
        if not cells:
            continue
        record = Record(index=index, collection="table")
        for position, cell in enumerate(cells[:MAX_COLUMNS]):
            name = names[position] if position < len(names) else f"column_{position + 1}"
            value = _cell_value(cell, base_url)
            if value in (None, ""):
                continue
            canonical = field_for_token(name) or name
            record.set(
                FieldValue(
                    name=canonical,
                    value=value,
                    raw=value,
                    type=infer_type(value),
                    confidence=_confidence(position, headers),
                    source=Source.TABLE,
                    selector=f"{css_path(table, root)} td:nth-child({position + 1})",
                    signals=[
                        base_signal(Source.TABLE, f"column {position + 1}"),
                        Signal("table header name", BONUS["name_exact"] if headers else 0.0, name),
                    ],
                )
            )
        if record.fields:
            records.append(record)
            index += 1
    return records


def _cell_value(cell: HtmlElement, base_url: str) -> object:
    link = cell.find(".//a")
    if link is not None and link.get("href") and not node_text(cell, limit=200):
        return absolutize(base_url, link.get("href"))
    img = cell.find(".//img")
    if img is not None and not node_text(cell, limit=200):
        return image_url(img, base_url)
    return node_text(cell, limit=4000)


def _column_names(headers: list[str]) -> list[str]:
    names: list[str] = []
    used: set[str] = set()
    for position, header in enumerate(headers):
        tokens = tokens_of(header)
        base = "_".join(tokens)[:48] or f"column_{position + 1}"
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        names.append(name)
    return names


def _confidence(position: int, headers: list[str]) -> float:
    base = 0.55 if headers else 0.35
    return round(base + (0.05 if position == 0 else 0.0), 4)


def summarize(root: HtmlElement) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for table in root.iter("table"):
        rows = table.findall(".//tr")
        cols = max((len(r.findall("./td")) + len(r.findall("./th")) for r in rows), default=0)
        if rows:
            out.append((css_path(table, root), len(rows), cols))
    return out
