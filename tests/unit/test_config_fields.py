"""The escape hatch: a pinned selector must resolve inside each record, not page-wide."""

from __future__ import annotations

import pytest

from uparse.config.schema import ExtractionConfig, FieldSpec
from uparse.core.models import PageModel
from uparse.extraction.engine import extract

BASE = "https://shop.test/list"
LISTING = """
<ul class="products">
  <li class="product"><h3><a href="/p/1" title="Full Name One">Clipped One...</a></h3>
    <span class="cost">£10.00</span><img src="/i/1.jpg"></li>
  <li class="product"><h3><a href="/p/2" title="Full Name Two">Clipped Two...</a></h3>
    <span class="cost">£20.00</span><img src="/i/2.jpg"></li>
  <li class="product"><h3><a href="/p/3" title="Full Name Three">Clipped Three...</a></h3>
    <span class="cost">£30.00</span><img src="/i/3.jpg"></li>
</ul>
"""


def _run(fields):
    page = PageModel(url=BASE, final_url=BASE, html=LISTING)
    return extract(page, ExtractionConfig(fields=fields)).records


def test_a_pinned_selector_varies_per_record():
    records = _run({"title": FieldSpec(selector="h3 a")})
    assert [r.get("title") for r in records] == [
        "Clipped One...",
        "Clipped Two...",
        "Clipped Three...",
    ]


def test_a_bare_selector_on_an_anchor_yields_text_not_href():
    assert _run({"title": FieldSpec(selector="h3 a")})[0].get("title") == "Clipped One..."


def test_a_url_field_still_yields_the_href():
    assert _run({"url": FieldSpec(selector="h3 a")})[0].get("url") == "https://shop.test/p/1"


def test_the_text_pseudo_attribute_forces_text():
    spec = FieldSpec(mode="attribute", selector="h3 a", attribute="text")
    assert _run({"url": spec})[0].get("url") == "Clipped One..."


def test_a_real_attribute_is_read_per_record():
    spec = FieldSpec(mode="attribute", selector="h3 a", attribute="title")
    assert [r.get("title") for r in _run({"title": spec})] == [
        "Full Name One",
        "Full Name Two",
        "Full Name Three",
    ]


def test_a_selector_that_misses_leaves_the_field_out_rather_than_borrowing():
    records = _run({"title": FieldSpec(selector="h3 a"), "brand": FieldSpec(selector=".nope")})
    assert all("brand" not in r for r in records)


@pytest.mark.parametrize("attribute", ["href", "src"])
def test_url_attributes_come_back_absolute(attribute):
    selector = "h3 a" if attribute == "href" else "img"
    spec = FieldSpec(mode="attribute", selector=selector, attribute=attribute)
    value = _run({"image": spec})[0].get("image")
    assert value.startswith("https://shop.test/")
