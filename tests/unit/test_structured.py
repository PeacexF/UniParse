from __future__ import annotations

from uparse.extraction import structured as sd
from uparse.extraction.htmlutil import parse

BASE = "https://shop.test/p/macbook-pro-14"


def test_jsonld_graph_is_flattened(fixture_html):
    root = parse(fixture_html("product_jsonld"), BASE)
    types = {t for obj in sd.jsonld(root) for t in sd.type_names(obj)}
    assert {"product", "breadcrumblist", "organization"} <= types


def test_jsonld_recognizes_record_types(fixture_html):
    root = parse(fixture_html("product_jsonld"), BASE)
    products = [o for o in sd.jsonld(root) if sd.is_record_type(o)]
    assert any(p.get("sku") == "MBP14-M3P" for p in products)


def test_concatenated_jsonld_blocks_are_recovered():
    root = parse('<script type="application/ld+json">{"@type":"A"}{"@type":"B"}</script>')
    assert [sd.type_names(o) for o in sd.jsonld(root)] == [["a"], ["b"]]


def test_microdata_nests_offers(fixture_html):
    root = parse(fixture_html("product_jsonld"), BASE)
    items = sd.microdata(root, BASE)
    assert len(items) == 1
    assert items[0]["offers"]["price"] == "1999.00"
    assert items[0]["releaseDate"] == "2026-03-01"


def test_microdata_resolves_relative_urls(fixture_html):
    root = parse(fixture_html("product_jsonld"), BASE)
    assert sd.microdata(root, BASE)[0]["image"] == "https://shop.test/img/mbp14.jpg"


def test_opengraph_and_canonical(fixture_html):
    root = parse(fixture_html("product_jsonld"), BASE)
    og = sd.opengraph(root, BASE)
    assert og["title"] == 'MacBook Pro 14"'
    assert og["image"] == "https://shop.test/img/mbp14.jpg"
    assert og["canonical"] == "https://shop.test/p/macbook-pro-14"


def test_embedded_state_is_found_without_hardcoding_the_framework(fixture_html):
    root = parse(fixture_html("product_jsonld"), BASE)
    keys = dict(sd.embedded_json(root))
    assert "__NEXT_DATA__" in keys
    assert keys["__INITIAL_STATE__"]["currency"] == "USD"


def test_embedded_scanner_skips_jsonld_scripts(fixture_html):
    root = parse(fixture_html("product_jsonld"), BASE)
    assert all("ld+json" not in key for key, _ in sd.embedded_json(root))
