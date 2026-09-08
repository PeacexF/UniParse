"""Cross-record alignment: fields discovered from what repeats, not from class vocabulary."""

from __future__ import annotations

from uparse.core.models import PageModel
from uparse.extraction import alignment as align
from uparse.extraction.collections import discover
from uparse.extraction.engine import extract
from uparse.extraction.htmlutil import parse

BASE = "https://feed.test/list"

# Hashed class names on purpose: nothing here can be named by the vocabulary.
OBFUSCATED = """
<main>
  <div class="sc-a1b2c3">
    <div class="sc-x9y8z7"><a href="/a">Rust ships a new borrow checker</a>
      <p class="sc-p0q1r2">A long summary that varies per record and says something real about it.</p>
      <span class="sc-d4e5f6">2026-03-01</span></div>
    <div class="sc-x9y8z7"><a href="/b">Python 3.15 lands free-threading by default</a>
      <p class="sc-p0q1r2">Another distinct summary, also long enough to read as a description.</p>
      <span class="sc-d4e5f6">2026-03-02</span></div>
    <div class="sc-x9y8z7"><a href="/c">Postgres 19 adds asynchronous IO everywhere</a>
      <p class="sc-p0q1r2">A third summary with different words and comparable length to the others.</p>
      <span class="sc-d4e5f6">2026-03-03</span></div>
  </div>
</main>
"""


def _records(html):
    return extract(PageModel(url=BASE, final_url=BASE, html=html)).records


def test_dates_are_found_without_a_class_name_to_go_on():
    records = _records(OBFUSCATED)
    assert len(records) == 3
    assert [r.get("date") for r in records] == [
        "2026-03-01T00:00:00",
        "2026-03-02T00:00:00",
        "2026-03-03T00:00:00",
    ]


def test_the_long_varying_column_becomes_the_description():
    first = _records(OBFUSCATED)[0]
    assert first.get("description").startswith("A long summary that varies")


def test_a_page_that_names_its_own_fields_keeps_those_names():
    html = """
    <ul class="grid">
      <li class="entry"><h3>Andorra</h3>
        <span class="entry-capital">Andorra la Vella</span>
        <span class="entry-population">84000</span></li>
      <li class="entry"><h3>Belize</h3>
        <span class="entry-capital">Belmopan</span>
        <span class="entry-population">327719</span></li>
      <li class="entry"><h3>Chad</h3>
        <span class="entry-capital">N'Djamena</span>
        <span class="entry-population">13670084</span></li>
    </ul>
    """
    records = _records(html)
    assert [r.get("capital") for r in records] == ["Andorra la Vella", "Belmopan", "N'Djamena"]
    assert [r.get("population") for r in records] == ["84000", "327719", "13670084"]


def test_a_column_repeating_one_label_is_not_a_field():
    html = """
    <ul class="grid">
      <li class="entry"><h3>Alpha</h3><span class="tag-thing">Read more</span></li>
      <li class="entry"><h3>Beta</h3><span class="tag-thing">Read more</span></li>
      <li class="entry"><h3>Gamma</h3><span class="tag-thing">Read more</span></li>
    </ul>
    """
    for record in _records(html):
        assert "thing" not in record.fields


def test_a_wrapper_restating_the_record_is_not_a_column():
    root = parse(OBFUSCATED, BASE)
    nodes = discover(root)[0].nodes
    selectors = {c.selector for record in align.candidates(nodes, BASE) for c in record}
    assert all(" > " in s or "." in s for s in selectors)
    for record in _records(OBFUSCATED):
        for value in record.to_dict().values():
            assert "Rust ships a new borrow checker A long summary" not in str(value)


def test_alignment_needs_enough_records_to_be_evidence():
    two = """
    <ul class="grid">
      <li class="entry"><h3>Alpha</h3><span class="e-x">2026-03-01</span></li>
      <li class="entry"><h3>Beta</h3><span class="e-x">2026-03-02</span></li>
    </ul>
    """
    root = parse(two, BASE)
    nodes = discover(root)[0].nodes
    assert align.candidates(nodes, BASE) == [[], []]
