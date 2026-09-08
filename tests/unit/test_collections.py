from __future__ import annotations

from uparse.extraction.collections import discover
from uparse.extraction.dom import fingerprint, similarity
from uparse.extraction.htmlutil import parse


def test_product_cards_beat_navigation(fixture_html):
    root = parse(fixture_html("product_listing"), "https://shop.test/laptops")
    top = discover(root)[0]
    assert top.count == 4
    assert top.label == "product"
    assert top.confidence > 0.8
    assert "product-card" in top.selector


def test_navigation_and_footer_are_not_collections(fixture_html):
    root = parse(fixture_html("product_listing"), "https://shop.test/laptops")
    selectors = " ".join(c.selector for c in discover(root))
    assert "menu" not in selectors
    assert "pagination" not in selectors


def test_hashed_css_module_classes_still_cluster(fixture_html):
    root = parse(fixture_html("messy_listing"), "https://shop.test/angebote")
    top = discover(root)[0]
    assert top.count == 3


def test_discovery_explains_itself(fixture_html):
    root = parse(fixture_html("product_listing"), "https://shop.test/laptops")
    names = {s.name for s in discover(root)[0].signals}
    assert "repeated siblings" in names
    assert "structural similarity" in names


def test_similarity_is_symmetric_and_bounded():
    root = parse(
        "<ul>"
        "<li class='row'><b>x</b></li>"
        "<li class='row'><b>y</b></li>"
        "<li class='promo'><span>q</span><span>r</span></li>"
        "</ul>"
    )
    a, b, c = (fingerprint(n) for n in root.cssselect("li"))
    assert similarity(a, b) == similarity(b, a) == 1.0
    assert 0.0 <= similarity(a, c) < 1.0


def test_missing_class_names_are_neutral_not_a_match():
    root = parse("<ul><li><b>x</b></li><li><b>y</b></li></ul>")
    a, b = (fingerprint(n) for n in root.cssselect("li"))
    assert 0.75 <= similarity(a, b) < 1.0


def test_single_element_is_not_a_collection():
    root = parse("<div><article>only one</article></div>")
    assert discover(root) == []
