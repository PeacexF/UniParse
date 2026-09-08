from __future__ import annotations

from uparse.core.models import Source
from uparse.extraction import fields as fld
from uparse.extraction.collections import discover
from uparse.extraction.htmlutil import parse
from uparse.processing.scoring import best_per_field

BASE = "https://shop.test/laptops"


def _best(html_text, base=BASE):
    root = parse(html_text, base)
    node = discover(root)[0].nodes[0]
    return best_per_field(fld.from_node(node, base, root))


def test_listing_card_yields_the_expected_fields(fixture_html):
    best = _best(fixture_html("product_listing"))
    assert {"title", "url", "price", "image", "rating", "description", "availability"} <= set(best)
    assert best["title"].value == 'MacBook Pro 14"'
    assert best["url"].value == "https://shop.test/p/mbp-14"


def test_class_names_drive_field_naming(fixture_html):
    best = _best(fixture_html("product_listing"))
    assert best["price"].source is Source.CLASSNAME
    assert best["price"].value == "$1,999.00"


def test_lazy_loaded_images_are_found(fixture_html):
    best = _best(fixture_html("messy_listing"), "https://shop.test/angebote/seite/2")
    assert best["image"].value.endswith("/bilder/1.jpg")


def test_record_anchor_becomes_the_url_without_class_hints(fixture_html):
    best = _best(fixture_html("messy_listing"), "https://shop.test/angebote/seite/2")
    assert best["url"].value == "https://shop.test/angebote/artikel/1"


def test_boilerplate_link_text_never_becomes_a_title():
    html = """
    <div class='list'>
      <div class='card'><h3>Real Title One</h3><a href='/1'>Read more</a></div>
      <div class='card'><h3>Real Title Two</h3><a href='/2'>Read more</a></div>
    </div>
    """
    assert _best(html)["title"].value == "Real Title One"


def test_itemprop_beats_a_bare_class_name():
    html = """
    <div class='list'>
      <div class='card'><span class='name'>wrong</span><span itemprop='name'>right</span></div>
      <div class='card'><span class='name'>wrong</span><span itemprop='name'>right too</span></div>
    </div>
    """
    best = _best(html)
    assert best["title"].value == "right"
    assert best["title"].source is Source.MICRODATA


def test_schema_object_maps_onto_canonical_fields():
    obj = {
        "@type": "Product",
        "name": "Thing",
        "offers": {"price": "10.00", "priceCurrency": "EUR"},
        "aggregateRating": {"ratingValue": "4.2", "reviewCount": 7},
    }
    best = best_per_field(fld.from_schema_object(obj, Source.JSONLD, BASE))
    assert best["title"].value == "Thing"
    assert best["price"].value == "10.00"
    assert best["currency"].value == "EUR"
    assert best["rating"].value == "4.2"
    assert best["reviews"].value == 7
