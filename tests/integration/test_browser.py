"""Integration tests for the TypeScript Playwright worker.

Everything here needs `make browser`; without it the suite is skipped, not failed.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from click.testing import CliRunner

from uparse.acquisition.browser import WORKER_DIR, BrowserAcquirer, WorkerProcess
from uparse.cli import main
from uparse.config.schema import BrowserConfig, Config, PaginationConfig
from uparse.core.errors import BlockedError, HttpError, NavigationTimeoutError, WorkerError
from uparse.runtime.protocol import PROTOCOL_VERSION

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def worker():
    process = WorkerProcess(BrowserConfig(timeout=15_000, concurrency=2))
    try:
        yield process
    finally:
        process.close()


@pytest.fixture
def acquirer():
    made: list[BrowserAcquirer] = []

    def _make(**browser: object) -> BrowserAcquirer:
        config = Config(browser=BrowserConfig(timeout=15_000, **browser), acquirer="browser")
        instance = BrowserAcquirer(config)
        made.append(instance)
        return instance

    yield _make
    for instance in made:
        instance.close()


@pytest.fixture
def run():
    def _run(*args):
        return CliRunner().invoke(main, list(args), catch_exceptions=False)

    return _run


# --- protocol -------------------------------------------------------------


def test_handshake_agrees_on_the_protocol_version(worker):
    data = worker.call("hello", {"protocol": PROTOCOL_VERSION, "headless": True})
    assert data["protocol"] == PROTOCOL_VERSION
    assert data["name"] == "uparse-browser"


def test_unknown_method_is_a_protocol_error_not_a_crash(worker, site_url):
    with pytest.raises(WorkerError):
        worker.call("teleport", {})
    # The worker is still usable afterwards.
    assert worker.call("navigate", {"url": f"{site_url}/page1.html"})["status"] == 200


def test_a_malformed_frame_does_not_desynchronize_the_stream(worker, site_url):
    worker._proc.stdin.write("this is not json\n")
    worker._proc.stdin.flush()
    assert worker.call("navigate", {"url": f"{site_url}/page1.html"})["title"] == "Catalog page 1"


def test_stdout_carries_protocol_frames_only():
    """console.log and a direct stdout write must both end up on stderr."""
    script = (
        "import('./dist/protocol.js').then((protocol) => {"
        "  protocol.guardStdout();"
        "  console.log('diagnostic noise');"
        "  process.stdout.write('more noise');"
        "  protocol.send({ id: 7, ok: true, data: { fine: true } });"
        "});"
    )
    done = subprocess.run(
        [sys.executable and "node", "--input-type=module", "-e", script],
        cwd=WORKER_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(done.stdout) == {"id": 7, "ok": True, "data": {"fine": True}}
    assert "diagnostic noise" in done.stderr
    assert "more noise" in done.stderr


# --- acquisition ----------------------------------------------------------


def test_javascript_rendered_records_are_invisible_to_plain_http(acquirer, site_url):
    page = acquirer().fetch(f"{site_url}/js.html")
    assert page.status == 200
    assert page.title == "JS catalog"
    assert "Nova Lamp" in page.html
    assert page.metadata["worker"] is True


def test_an_http_error_status_surfaces_as_an_http_error(acquirer, site_url):
    with pytest.raises(HttpError) as caught:
        acquirer().fetch(f"{site_url}/missing.html")
    assert caught.value.status == 404


def test_a_challenge_page_is_reported_never_solved(acquirer, site_url):
    with pytest.raises(BlockedError):
        acquirer().fetch(f"{site_url}/challenge.html")


def test_an_unreachable_host_fails_the_page_not_the_worker(acquirer, site_url):
    instance = acquirer()
    with pytest.raises(Exception) as caught:
        instance.fetch("http://127.0.0.1:9/nothing")
    assert not isinstance(caught.value, BlockedError)
    # The same worker still serves the next page: a poisoned page is discarded, not pooled.
    assert instance.fetch(f"{site_url}/page1.html").status == 200


def test_a_navigation_timeout_does_not_poison_the_page_pool(acquirer, site_url):
    instance = acquirer(timeout=1_000, wait_until="load")
    with pytest.raises(NavigationTimeoutError):
        instance.worker.call("navigate", {"url": f"{site_url}/page1.html", "timeout": 1})
    assert instance.fetch(f"{site_url}/page1.html").status == 200


def test_blocked_resources_never_reach_the_network(acquirer, site_url):
    served = acquirer()
    page = served.fetch(f"{site_url}/image.html")
    assert _pixel_width(served, page) == 1

    blocked = acquirer(block_images=True)
    page = blocked.fetch(f"{site_url}/image.html")
    assert _pixel_width(blocked, page) == 0


def _pixel_width(instance: BrowserAcquirer, page) -> int:
    result = instance.worker.call(
        "evaluate",
        {
            "pageId": page.metadata["pageId"],
            "expression": "document.getElementById('pixel').naturalWidth",
        },
    )
    return int(result["result"])


def test_infinite_scroll_collects_what_lazy_loading_appends(acquirer, site_url):
    config = Config(
        browser=BrowserConfig(timeout=15_000),
        pagination=PaginationConfig(infinite_scroll=True, max_scrolls=6),
        acquirer="browser",
    )
    instance = BrowserAcquirer(config)
    try:
        page = instance.fetch(f"{site_url}/scroll.html")
    finally:
        instance.close()
    assert page.metadata["scrolls"] >= 1
    assert "Item 6" in page.html


def test_a_killed_page_does_not_take_the_worker_with_it(worker, site_url):
    page_id = worker.call("navigate", {"url": f"{site_url}/page1.html"})["pageId"]
    worker.call("close", {"pageId": page_id})
    with pytest.raises(WorkerError):
        worker.call("content", {"pageId": page_id})
    assert worker.call("navigate", {"url": f"{site_url}/page2.html"})["status"] == 200


# --- the whole pipeline ---------------------------------------------------


def test_the_cli_scrapes_a_javascript_page_through_chromium(run, site_url, tmp_path):
    out = tmp_path / "js.json"
    result = run(f"{site_url}/js.html", "--no-pagination", "--output", str(out))
    assert result.exit_code == 0
    rows = json.loads(out.read_text())
    assert [row["title"] for row in rows] == ["Nova Lamp", "Orion Desk", "Pico Cable"]
    assert rows[0]["price"] == 39.5
    assert rows[0]["url"] == f"{site_url}/p/lm-1.html"


def test_the_cli_follows_pagination_through_chromium(run, site_url, tmp_path):
    out = tmp_path / "products.jsonl"
    result = run(f"{site_url}/page1.html", "--output", str(out))
    assert result.exit_code == 0
    titles = [json.loads(line)["title"] for line in out.read_text().splitlines()]
    assert titles[0] == "Alpha Keyboard"
    assert titles[-1] == "Zeta Dock"
    assert len(titles) == 6


def test_inspect_runs_against_a_rendered_page(run, site_url):
    result = run("inspect", f"{site_url}/js.html", "--schema")
    schema = json.loads(result.stdout)
    assert schema["price"]["type"] == "number"
    assert schema["title"]["confidence"] > 0.5
