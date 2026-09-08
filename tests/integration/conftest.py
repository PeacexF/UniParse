from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SITE = Path(__file__).parent / "site"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        del fmt, args


@dataclass
class Site:
    url: str
    root: Path


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    """A real HTTP server over a generated three-page paginated site."""
    root = tmp_path_factory.mktemp("site")
    _build_site(root)
    handler = partial(_QuietHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Site(url=f"http://127.0.0.1:{server.server_port}", root=root)
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def site_url(site):
    return site.url


def _build_site(root: Path) -> None:
    items = [
        ("Alpha Keyboard", 49.99, "kb-1"),
        ("Beta Mouse", 29.50, "ms-1"),
        ("Gamma Monitor", 219.00, "mn-1"),
        ("Delta Webcam", 89.90, "wc-1"),
        ("Epsilon Headset", 129.00, "hs-1"),
        ("Zeta Dock", 179.00, "dk-1"),
    ]
    pages = [items[0:2], items[2:4], items[4:6]]
    for number, chunk in enumerate(pages, 1):
        cards = "\n".join(
            f"""
      <div class="product-card">
        <h2 class="product-title"><a href="/p/{sku}.html">{name}</a></h2>
        <span class="price">${price:,.2f}</span>
        <img class="thumb" src="/img/{sku}.jpg" alt="{name}">
        <span class="stock">In Stock</span>
      </div>"""
            for name, price, sku in chunk
        )
        nxt = (
            f'<a class="next" rel="next" href="/page{number + 1}.html">Next ›</a>'
            if number < len(pages)
            else '<a class="prev" href="/page1.html">Previous</a>'
        )
        (root / f"page{number}.html").write_text(
            f"""<!doctype html>
<html lang="en"><head><title>Catalog page {number}</title></head>
<body>
  <main>
    <div class="product-grid">{cards}
    </div>
    <nav class="pagination">{nxt}</nav>
  </main>
</body></html>
""",
            encoding="utf-8",
        )
    (root / "index.html").write_text('<html><body><a href="/page1.html">Catalog</a></body></html>')
    (root / "empty.html").write_text("<html><body><p>Nothing here.</p></body></html>")
