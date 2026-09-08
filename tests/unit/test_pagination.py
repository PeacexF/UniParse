from __future__ import annotations

import pytest

from uparse.config.schema import PaginationConfig
from uparse.core.models import PageModel
from uparse.extraction.htmlutil import parse
from uparse.navigation.pagination import find_next, next_url_by_pattern

BASE = "https://shop.test/laptops?page=2"


def _next(html_text, url=BASE, config=None, seen=None):
    page = PageModel(url=url, html=html_text)
    return find_next(parse(html_text, url), page, config or PaginationConfig(), seen=seen)


def test_rel_next_wins(fixture_html):
    hint = _next(fixture_html("product_listing"))
    assert hint.url == "https://shop.test/laptops?page=3"
    assert hint.method == "rel=next"
    assert hint.confidence > 0.9


def test_link_text_is_used_without_rel():
    html = "<nav class='pagination'><a href='/p/3'>Next page</a></nav>"
    hint = _next(html)
    assert hint.url == "https://shop.test/p/3"
    assert hint.method == "link text"


@pytest.mark.parametrize("label", ["Weiter", "Suivant", "Следующая", "下一页", "Older posts"])
def test_localized_next_labels(label):
    hint = _next(f"<nav class='pagination'><a href='/p/3'>{label}</a></nav>")
    assert hint is not None


def test_previous_link_is_never_followed():
    html = "<nav class='pagination'><a href='/p/1'>Previous</a></nav>"
    assert _next(html) is None


def test_numbered_pager_follows_the_next_number():
    html = """
    <nav class='pagination'>
      <a href='/l?page=1'>1</a><a class='current' href='/l?page=2'>2</a>
      <a href='/l?page=3'>3</a>
    </nav>
    """
    hint = _next(html)
    assert hint.url == "https://shop.test/l?page=3"


def test_url_increment_only_when_a_pager_exists():
    assert _next("<div><a href='/x'>elsewhere</a></div>") is None
    hint = _next("<nav class='pager'><a href='#'>…</a></nav>")
    assert hint.url == "https://shop.test/laptops?page=3"
    assert hint.method == "url pattern"


def test_already_visited_urls_are_skipped(fixture_html):
    seen = {"https://shop.test/laptops?page=3"}
    assert _next(fixture_html("product_listing"), seen=seen) is None


def test_configured_selector_overrides_everything(fixture_html):
    config = PaginationConfig(selector="a.prev")
    hint = _next(fixture_html("product_listing"), config=config)
    assert hint.method == "configured"
    assert hint.url == "https://shop.test/laptops?page=1"


def test_url_template_override():
    config = PaginationConfig(url_template="https://shop.test/l?page={page}")
    hint = _next("<div></div>", config=config)
    assert hint.url == "https://shop.test/l?page=3"


def test_disabled_pagination_returns_nothing(fixture_html):
    assert _next(fixture_html("product_listing"), config=PaginationConfig(enabled=False)) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.test/l?page=2", "https://x.test/l?page=3"),
        ("https://x.test/l?p=9", "https://x.test/l?p=10"),
        ("https://x.test/page/4", "https://x.test/page/5"),
        ("https://x.test/page/4/", "https://x.test/page/5/"),
        ("https://x.test/l?offset=20", "https://x.test/l?offset=40"),
        ("https://x.test/l", None),
    ],
)
def test_url_pattern_increment(url, expected):
    assert next_url_by_pattern(url) == expected


def test_last_numbered_page_stops_the_run():
    html = """
    <nav class='pagination'>
      <a href='/l?page=1'>1</a><a class='current' href='/l?page=2'>2</a>
    </nav>
    """
    assert _next(html) is None


def test_prev_only_pager_stops_the_run():
    assert _next("<nav class='pagination'><a href='/p/1'>Previous</a></nav>") is None
