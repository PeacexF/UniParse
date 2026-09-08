"""Parallel sources, per-host politeness, robots.txt and listing→detail following."""

from __future__ import annotations

import json
import sqlite3

import pytest
from click.testing import CliRunner

from uparse.cli import main


@pytest.fixture
def run():
    def _run(*args):
        return CliRunner().invoke(main, list(args), catch_exceptions=False)

    return _run


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- concurrency ----------------------------------------------------------


def test_workers_do_not_change_the_output(run, site_url, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text("\n".join(f"{site_url}/page{n}.html" for n in (1, 2, 3)))
    serial, parallel = tmp_path / "serial.jsonl", tmp_path / "parallel.jsonl"
    run(str(urls), "--no-browser", "--no-pagination", "-j", "1", "--output", str(serial))
    run(str(urls), "--no-browser", "--no-pagination", "-j", "8", "--output", str(parallel))
    assert serial.read_text() == parallel.read_text()


def test_every_source_is_processed_in_parallel(run, site_url, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text("\n".join(f"{site_url}/page{n}.html" for n in (1, 2, 3)))
    out = tmp_path / "products.jsonl"
    result = run(str(urls), "--no-browser", "--no-pagination", "-j", "4", "--output", str(out))
    assert result.exit_code == 0
    assert len(_rows(out)) == 6


def test_a_failure_in_one_worker_leaves_the_others_alone(run, site_url, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site_url}/missing.html\n{site_url}/page1.html\n{site_url}/page2.html\n")
    out = tmp_path / "products.jsonl"
    run(str(urls), "--no-browser", "--no-pagination", "-j", "4", "--output", str(out))
    assert len(_rows(out)) == 4


# --- robots.txt -----------------------------------------------------------


def test_a_disallowed_path_is_not_fetched(run, site_url, tmp_path):
    out, db = tmp_path / "records.jsonl", tmp_path / "job.db"
    run(f"{site_url}/private/secret.html", "--no-browser", "--output", str(out), "--db", str(db))
    assert not _rows(out)
    with sqlite3.connect(db) as conn:
        codes = [row[0] for row in conn.execute("SELECT error_type FROM errors")]
    assert codes == ["ROBOTS_DISALLOWED"]


def test_no_robots_opts_out(run, site_url, tmp_path):
    out = tmp_path / "records.jsonl"
    run(f"{site_url}/private/secret.html", "--no-browser", "--no-robots", "--output", str(out))
    assert [row["title"] for row in _rows(out)] == ["Hidden One", "Hidden Two"]


def test_an_allowed_path_is_unaffected(run, site_url, tmp_path):
    out = tmp_path / "records.jsonl"
    run(f"{site_url}/page1.html", "--no-browser", "--no-pagination", "--output", str(out))
    assert len(_rows(out)) == 2


# --- follow ---------------------------------------------------------------


def test_following_merges_the_detail_page_into_the_record(run, site_url, tmp_path):
    out = tmp_path / "products.jsonl"
    result = run(
        f"{site_url}/page1.html",
        "--no-browser",
        "--no-pagination",
        "--follow",
        "--output",
        str(out),
    )
    assert result.exit_code == 0
    first = _rows(out)[0]
    assert first["sku"] == "KB-1"
    assert first["brand"] == "Testronics"
    assert first["availability"] == "In stock (7 available)"
    assert first["description"].startswith("The Alpha Keyboard is built")


def test_following_keeps_the_listing_title_and_url(run, site_url, tmp_path):
    out = tmp_path / "products.jsonl"
    run(
        f"{site_url}/page1.html",
        "--no-browser",
        "--no-pagination",
        "--follow",
        "--output",
        str(out),
    )
    first = _rows(out)[0]
    assert first["title"] == "Alpha Keyboard"  # not "Alpha Keyboard | Test Shop"
    assert first["url"] == f"{site_url}/p/kb-1.html"


def test_the_follow_budget_is_respected(run, site_url, tmp_path):
    config = tmp_path / "job.jsonc"
    out = tmp_path / "products.jsonl"
    config.write_text(
        f"""{{
  "sources": {{ "type": "url", "url": "{site_url}/page1.html" }},
  "acquirer": "http",
  "browser": {{ "enabled": false }},
  "pagination": {{ "enabled": false }},
  "follow": {{ "enabled": true, "max_pages": 1 }},
  "output": {{ "format": "jsonl", "path": "{out}" }}
}}"""
    )
    run(str(config))
    rows = _rows(out)
    assert sum(1 for row in rows if "sku" in row) == 1


def test_records_without_a_link_are_left_alone(run, site_url, tmp_path):
    out = tmp_path / "records.jsonl"
    result = run(
        f"{site_url}/private/secret.html",
        "--no-browser",
        "--no-robots",
        "--follow",
        "--output",
        str(out),
    )
    assert result.exit_code == 0
    assert [row["title"] for row in _rows(out)] == ["Hidden One", "Hidden Two"]
