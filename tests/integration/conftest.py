from __future__ import annotations

import base64
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
    _build_detail_pages(root, items)
    _build_robots(root)
    _build_browser_pages(root)


def _build_detail_pages(root: Path, items: list[tuple[str, float, str]]) -> None:
    """One page per product, with the label/value table detail pages usually carry."""
    (root / "p").mkdir(exist_ok=True)
    for name, price, sku in items:
        (root / "p" / f"{sku}.html").write_text(
            f"""<!doctype html>
<html lang="en"><head><title>{name} | Test Shop</title>
<meta property="og:title" content="{name}"></head>
<body>
  <h1 class="product-title">{name}</h1>
  <p class="description">The {name} is built for people who test parsers.</p>
  <table class="specs">
    <tr><th>SKU</th><td>{sku.upper()}</td></tr>
    <tr><th>Brand</th><td>Testronics</td></tr>
    <tr><th>Availability</th><td>In stock (7 available)</td></tr>
  </table>
  <span class="price">${price:,.2f}</span>
</body></html>
""",
            encoding="utf-8",
        )


def _build_robots(root: Path) -> None:
    (root / "robots.txt").write_text("User-agent: *\nDisallow: /private/\n", encoding="utf-8")
    (root / "private").mkdir(exist_ok=True)
    (root / "private" / "secret.html").write_text(
        "<html><body><ul>"
        "<li class='product'><h2>Hidden One</h2><span class='price'>$1.00</span></li>"
        "<li class='product'><h2>Hidden Two</h2><span class='price'>$2.00</span></li>"
        "</ul></body></html>",
        encoding="utf-8",
    )


# A 1x1 PNG. Real bytes, so a blocked request is visibly different from a served one.
PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _build_browser_pages(root: Path) -> None:
    """Pages that only mean something once JavaScript has run."""
    (root / "pixel.png").write_bytes(PIXEL_PNG)

    # Nothing in the markup; the whole catalog is written by a script.
    (root / "js.html").write_text(
        """<!doctype html>
<html lang="en"><head><title>JS catalog</title></head>
<body>
  <div class="product-grid" id="grid"></div>
  <script>
    const items = [["Nova Lamp", "39.50", "lm-1"], ["Orion Desk", "249.00", "dk-9"],
                   ["Pico Cable", "9.99", "cb-3"]];
    addEventListener('DOMContentLoaded', () => {
      document.getElementById('grid').innerHTML = items.map(([name, price, sku]) => `
        <div class="product-card">
          <h2 class="product-title"><a href="/p/${sku}.html">${name}</a></h2>
          <span class="price">$${price}</span>
        </div>`).join('');
    });
  </script>
</body></html>
""",
        encoding="utf-8",
    )

    # Appends a batch every time the page is scrolled to the bottom, three times over.
    (root / "scroll.html").write_text(
        """<!doctype html>
<html lang="en"><head><title>Endless catalog</title></head>
<body>
  <div class="product-grid" id="grid"></div>
  <div style="height:2400px"></div>
  <script>
    let batch = 0;
    function append() {
      if (batch >= 3) return;
      const grid = document.getElementById('grid');
      for (let i = 0; i < 2; i++) {
        const n = batch * 2 + i + 1;
        grid.insertAdjacentHTML('beforeend',
          `<div class="product-card">
             <h2 class="product-title"><a href="/p/s${n}.html">Item ${n}</a></h2>
             <span class="price">$${n}.00</span>
           </div>`);
      }
      batch++;
      document.body.insertAdjacentHTML('beforeend', '<div style="height:1200px"></div>');
    }
    append();
    addEventListener('scroll', append);
  </script>
</body></html>
""",
        encoding="utf-8",
    )

    (root / "image.html").write_text(
        '<html><head><title>Image</title></head><body><img id="pixel" src="/pixel.png"></body></html>',
        encoding="utf-8",
    )

    # Detected and reported. UniParse never tries to pass a challenge.
    (root / "challenge.html").write_text(
        "<html><head><title>Just a moment...</title></head>"
        '<body><div class="cf-browser-verification">Checking your browser before accessing</div>'
        "</body></html>",
        encoding="utf-8",
    )
