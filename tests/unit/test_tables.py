"""Table extraction: column alignment under spans, and layout tables staying out of the way."""

from __future__ import annotations

from uparse.core.models import PageModel
from uparse.extraction import tables as tbl
from uparse.extraction.engine import extract
from uparse.extraction.htmlutil import parse

BASE = "https://data.test/companies"

# A row header <th> plus rowspan/colspan: the shape that shifted every value by one.
SPANNED = """
<table class="wikitable">
  <tr><th rowspan="2">Rank</th><th rowspan="2">Name</th><th colspan="2">USD (bn)</th>
      <th rowspan="2">Country</th></tr>
  <tr><th>Revenue</th><th>Profit</th></tr>
  <tr><th>1</th><td>Amazon</td><td>716</td><td>79.9</td><td rowspan="2">United States</td></tr>
  <tr><th>2</th><td>Walmart</td><td>713</td><td>21.8</td></tr>
  <tr><th>3</th><td>State Grid</td><td>545</td><td>9.2</td><td>China</td></tr>
</table>
"""


def _records(html):
    return extract(PageModel(url=BASE, final_url=BASE, html=html)).records


def test_a_row_header_does_not_shift_the_columns():
    # "Name" and "Country" canonicalize to the vocabulary's names for those fields.
    first = _records(SPANNED)[0]
    assert first.get("rank") == "1"
    assert first.get("title") == "Amazon"
    assert first.get("revenue") == "716"
    assert first.get("profit") == "79.9"


def test_a_rowspan_carries_its_value_into_the_next_row():
    records = _records(SPANNED)
    assert records[0].get("location") == "United States"
    assert records[1].get("location") == "United States"
    assert records[2].get("location") == "China"


def test_a_multi_row_header_names_the_columns_from_the_top_row():
    root = parse(SPANNED, BASE)
    candidate = tbl.best(root, min_rows=2)
    assert candidate is not None
    assert candidate.columns == 5
    assert candidate.rows == 3


def test_a_layout_table_scores_below_a_real_one():
    layout = """
    <table id="page"><tr><td>
      <table class="items">
        <tr><td>a</td><td>b</td><td>c</td></tr>
        <tr><td colspan="2">sub</td></tr>
        <tr><td>d</td><td>e</td><td>f</td></tr>
        <tr><td colspan="2">sub</td></tr>
      </table>
    </td></tr></table>
    """
    outer = tbl.best(parse(layout, BASE), min_rows=2)
    real = tbl.best(parse(SPANNED, BASE), min_rows=2)
    assert outer is not None and real is not None
    assert real.confidence > outer.confidence


def test_a_high_confidence_collection_beats_a_layout_table():
    html = """
    <table id="shell"><tr><td>
      <ul class="products">
        <li class="product"><h3><a href="/1">Alpha Keyboard</a></h3><span class="price">$10</span></li>
        <li class="product"><h3><a href="/2">Beta Mouse</a></h3><span class="price">$20</span></li>
        <li class="product"><h3><a href="/3">Gamma Dock</a></h3><span class="price">$30</span></li>
      </ul>
    </td></tr></table>
    """
    result = extract(PageModel(url=BASE, final_url=BASE, html=html))
    assert result.strategy == "dom"
    assert [r.get("title") for r in result.records] == [
        "Alpha Keyboard",
        "Beta Mouse",
        "Gamma Dock",
    ]


def test_a_real_data_table_still_wins_over_its_rows_as_a_collection():
    assert extract(PageModel(url=BASE, final_url=BASE, html=SPANNED)).strategy == "table"
