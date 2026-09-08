from __future__ import annotations

import shutil
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


def pytest_collection_modifyitems(config, items):
    """Browser-marked tests need the compiled worker; skip rather than fail without it."""
    del config
    from uparse.acquisition.browser import WORKER_ENTRY

    if WORKER_ENTRY.exists() and shutil.which("node"):
        return
    skip = pytest.mark.skip(reason="browser worker not built (run `make setup && make browser`)")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)
