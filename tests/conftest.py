from __future__ import annotations

from pathlib import Path

import pytest

from uparse.core.models import PageModel

FIXTURES = Path(__file__).parent / "fixtures" / "html"


@pytest.fixture
def fixture_html():
    def _load(name: str) -> str:
        return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")

    return _load


@pytest.fixture
def fixture_page(fixture_html):
    def _page(name: str, url: str = "https://shop.test/listing") -> PageModel:
        return PageModel(url=url, html=fixture_html(name), status=200)

    return _page
