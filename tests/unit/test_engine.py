from __future__ import annotations

from uparse.config.schema import ExtractionConfig
from uparse.core.models import Source
from uparse.extraction.engine import extract


def test_listing_uses_dom_discovery(fixture_page):
    result = extract(fixture_page("product_listing", "https://shop.test/laptops?page=2"))
    assert result.strategy == "dom"
    assert len(result.records) == 4
    first = result.records[0].to_dict()
    assert first["title"] == 'MacBook Pro 14"'
    assert first["price"] == 1999.0
    assert first["rating"] == 4.8
    assert first["url"] == "https://shop.test/p/mbp-14"


def test_detail_page_merges_into_one_record(fixture_page):
    result = extract(fixture_page("product_jsonld", "https://shop.test/p/macbook-pro-14"))
    assert len(result.records) == 1
    data = result.records[0].to_dict()
    assert data["sku"] == "MBP14-M3P"
    assert data["price"] == 1999.0
    assert data["availability"] == "in stock"


def test_values_are_normalized_not_raw(fixture_page):
    record = extract(fixture_page("product_listing")).records[0]
    assert isinstance(record.to_dict()["price"], float)
    assert record.fields["price"].raw == "$1,999.00"


def test_table_page_becomes_rows(fixture_page):
    result = extract(fixture_page("news_table", "https://docs.test/releases"))
    assert result.strategy == "table"
    assert len(result.records) == 3
    assert result.records[0].get("version") == "1.0"
    assert result.records[1].get("price") == 9.99


def test_localized_prices_and_relative_urls(fixture_page):
    result = extract(fixture_page("messy_listing", "https://shop.test/angebote/seite/2"))
    prices = [r.get("price") for r in result.records]
    assert prices == [1299.0, 49.95, 1099.5]
    assert result.records[0].get("url") == "https://shop.test/angebote/artikel/1"


def test_explicit_selector_overrides_inference(fixture_page):
    cfg = ExtractionConfig(fields={"title": {"selector": ".desc"}, "price": "auto"})
    result = extract(fixture_page("product_listing"), cfg)
    assert result.records[0].to_dict()["title"].startswith("Apple M3 Pro")
    assert result.records[0].fields["title"].source is Source.CONFIG
    assert result.records[0].fields["title"].confidence == 1.0


def test_configured_fields_restrict_the_output(fixture_page):
    cfg = ExtractionConfig(fields={"title": "auto", "price": "auto"})
    assert set(extract(fixture_page("product_listing"), cfg).records[0].to_dict()) == {
        "title",
        "price",
    }


def test_schema_reports_types_and_confidence(fixture_page):
    schema = extract(fixture_page("product_listing")).schema
    assert str(schema["price"][0]) == "number"
    assert str(schema["url"][0]) == "url"
    assert all(0.0 < conf <= 1.0 for _, conf in schema.values())


def test_empty_page_yields_nothing(fixture_page):
    from uparse.core.models import PageModel

    result = extract(PageModel(url="https://x.test/", html="<html><body></body></html>"))
    assert result.records == []
    assert result.strategy == "none"


def test_provenance_survives_to_the_record(fixture_page):
    record = extract(fixture_page("product_listing")).records[0]
    prov = record.fields["price"].provenance()
    assert prov["source"] == "classname"
    assert prov["selector"]
    assert any(s["name"] == "collection consistency" for s in prov["signals"])
