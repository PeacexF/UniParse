from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from lxml.html import HtmlElement

from uparse.core.models import FieldValue, Record, Signal, Source
from uparse.extraction.htmlutil import absolutize, css_path, image_url, node_text
from uparse.extraction.vocabulary import field_for_token, tokens_of
from uparse.processing.normalize import infer_type
from uparse.processing.scoring import BONUS, base_signal

MAX_COLUMNS = 60
MAX_SPAN = 20
_CELL_TAGS = frozenset({"td", "th"})


@dataclass(slots=True)
class TableCandidate:
    """A `<table>` with a judgement about whether it holds data or just layout."""

    node: HtmlElement
    confidence: float
    rows: int
    columns: int
    signals: list[Signal]


def extract(root: HtmlElement, base_url: str, *, min_rows: int = 2) -> list[Record]:
    candidate = best(root, min_rows=min_rows)
    return [] if candidate is None else rows_of(candidate, base_url, root)


def best(root: HtmlElement, *, min_rows: int = 2) -> TableCandidate | None:
    """Highest-confidence data table on the page, or None when they all look like layout."""
    ranked = sorted(_candidates(root, min_rows), key=lambda c: (-c.confidence, -c.rows))
    return ranked[0] if ranked else None


def _candidates(root: HtmlElement, min_rows: int) -> list[TableCandidate]:
    out: list[TableCandidate] = []
    for table in root.iter("table"):
        grid = _grid(table)
        header_rows = _header_rows(grid)
        body = grid[len(header_rows) :]
        if len(body) < min_rows:
            continue
        widths = [len([c for c in row if c is not None]) for row in body]
        modal = Counter(widths).most_common(1)[0][0] if widths else 0
        if modal < 2:
            continue
        out.append(_score(table, header_rows, body, widths, modal))
    return out


def _score(
    table: HtmlElement,
    header_rows: list[list[HtmlElement | None]],
    body: list[list[HtmlElement | None]],
    widths: list[int],
    modal: int,
) -> TableCandidate:
    """A data table has a header, even columns and no table nested inside it."""
    signals: list[Signal] = []
    confidence = 0.30
    signals.append(Signal("table with rows", 0.30, f"{len(body)} rows × {modal} columns"))

    if header_rows:
        signals.append(Signal("header row", 0.25, f"{len(header_rows)} header row(s)"))
        confidence += 0.25

    even = sum(1 for w in widths if w == modal) / len(widths)
    signals.append(Signal("even column count", round(0.25 * even, 3), f"{even:.0%} of rows"))
    confidence += 0.25 * even

    nested = sum(1 for _ in table.iterdescendants("table"))
    if nested:
        signals.append(Signal("tables nested inside", -0.30, f"{nested} nested"))
        confidence -= 0.30
    if next(table.iterancestors("table"), None) is not None:
        signals.append(Signal("nested in another table", -0.15, "layout scaffolding"))
        confidence -= 0.15
    if modal >= 3 and len(body) >= 3:
        signals.append(Signal("tabular shape", 0.10, ""))
        confidence += 0.10

    return TableCandidate(
        node=table,
        confidence=round(max(0.0, min(confidence, 1.0)), 4),
        rows=len(body),
        columns=modal,
        signals=signals,
    )


def _grid(table: HtmlElement) -> list[list[HtmlElement | None]]:
    """Expand colspan/rowspan so column N of every row means the same thing.

    Without this a `<th scope="row">` or a merged cell shifts every value after it into
    its neighbour's column, which is worse than extracting nothing.
    """
    grid: list[list[HtmlElement | None]] = []
    for index, tr in enumerate(table.findall(".//tr")):
        while len(grid) <= index:
            grid.append([])
        row = grid[index]
        column = 0
        for cell in tr:
            if not isinstance(cell.tag, str) or cell.tag not in _CELL_TAGS:
                continue
            while column < len(row) and row[column] is not None:
                column += 1
            colspan = _span(cell, "colspan")
            rowspan = _span(cell, "rowspan")
            for down in range(rowspan):
                while len(grid) <= index + down:
                    grid.append([])
                target = grid[index + down]
                for across in range(colspan):
                    at = column + across
                    while len(target) <= at:
                        target.append(None)
                    if target[at] is None:
                        target[at] = cell
            column += colspan
    return [row[:MAX_COLUMNS] for row in grid if any(c is not None for c in row)]


def _span(cell: HtmlElement, attr: str) -> int:
    try:
        return max(1, min(int(cell.get(attr) or 1), MAX_SPAN))
    except ValueError:
        return 1


def _header_rows(grid: list[list[HtmlElement | None]]) -> list[list[HtmlElement | None]]:
    """Leading rows made entirely of <th>. Multi-row headers are merged per column."""
    head: list[list[HtmlElement | None]] = []
    for row in grid:
        cells = [c for c in row if c is not None]
        if not cells or any(c.tag != "th" for c in cells):
            break
        head.append(row)
    return head


def _headers(header_rows: list[list[HtmlElement | None]]) -> list[str]:
    """Name each column from the most specific header cell that covers only that column.

    A cell spanning several columns labels a *group* ("USD (in billions)"), so the name
    for a column is the deepest single-column header above it.
    """
    if not header_rows:
        return []
    width = max(len(row) for row in header_rows)
    out: list[str] = []
    for column in range(width):
        cells = [
            row[column] for row in header_rows if column < len(row) and row[column] is not None
        ]
        own = [c for c in reversed(cells) if _span(c, "colspan") == 1 and node_text(c, limit=200)]
        fallback = [c for c in cells if node_text(c, limit=200)]
        chosen = own[0] if own else (fallback[0] if fallback else None)
        out.append(node_text(chosen, limit=200) if chosen is not None else "")
    return out


def rows_of(candidate: TableCandidate, base_url: str, root: HtmlElement) -> list[Record]:
    table = candidate.node
    grid = _grid(table)
    header_rows = _header_rows(grid)
    headers = _headers(header_rows)
    names = _column_names(headers)
    table_selector = css_path(table, root)

    records: list[Record] = []
    for row in grid[len(header_rows) :]:
        cells = [c for c in row if c is not None]
        if not cells or all(c.tag == "th" for c in cells):
            continue  # a repeated header or a section divider, not data
        record = Record(index=len(records), collection="table")
        record.node = _row_node(cells)
        for position, cell in enumerate(row[:MAX_COLUMNS]):
            if cell is None:
                continue
            name = names[position] if position < len(names) else f"column_{position + 1}"
            value = _cell_value(cell, base_url)
            if value in (None, ""):
                continue
            record.set(
                FieldValue(
                    name=field_for_token(name) or name,
                    value=value,
                    raw=value,
                    type=infer_type(value),
                    confidence=_confidence(position, headers, candidate.confidence),
                    source=Source.TABLE,
                    selector=f"{table_selector} tr > *:nth-child({position + 1})",
                    signals=[
                        base_signal(Source.TABLE, f"column {position + 1}"),
                        Signal("table header name", BONUS["name_exact"] if headers else 0.0, name),
                    ],
                )
            )
        if record.fields:
            records.append(record)
    return records


def _row_node(cells: list[HtmlElement]) -> HtmlElement | None:
    parent = cells[0].getparent()
    return parent if parent is not None and parent.tag == "tr" else None


def _cell_value(cell: HtmlElement, base_url: str) -> object:
    text = node_text(cell, limit=4000)
    if text:
        return text
    link = cell.find(".//a")
    if link is not None and link.get("href"):
        return absolutize(base_url, link.get("href"))
    img = cell.find(".//img")
    if img is not None:
        return image_url(img, base_url)
    return text


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


def _confidence(position: int, headers: list[str], table_confidence: float) -> float:
    base = 0.55 if headers else 0.35
    return round(min(base + (0.05 if position == 0 else 0.0), table_confidence + 0.15), 4)


def summarize(root: HtmlElement) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for table in root.iter("table"):
        grid = _grid(table)
        cols = max((len(r) for r in grid), default=0)
        if grid:
            out.append((css_path(table, root), len(grid), cols))
    return out
