from __future__ import annotations

import csv
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


def test_scrape_follows_pagination_and_writes_json(run, site_url, tmp_path):
    out = tmp_path / "products.json"
    result = run(f"{site_url}/page1.html", "--no-browser", "--output", str(out))
    assert result.exit_code == 0
    rows = json.loads(out.read_text())
    assert len(rows) == 6
    assert rows[0]["title"] == "Alpha Keyboard"
    assert rows[0]["price"] == 49.99
    assert rows[-1]["title"] == "Zeta Dock"


def test_max_pages_limits_the_crawl(run, site_url, tmp_path):
    out = tmp_path / "products.jsonl"
    run(f"{site_url}/page1.html", "--no-browser", "--max-pages", "2", "--output", str(out))
    assert len(out.read_text().strip().splitlines()) == 4


def test_no_pagination_stays_on_the_first_page(run, site_url, tmp_path):
    out = tmp_path / "products.jsonl"
    run(f"{site_url}/page1.html", "--no-browser", "--no-pagination", "--output", str(out))
    assert len(out.read_text().strip().splitlines()) == 2


def test_fields_flag_restricts_columns(run, site_url, tmp_path):
    out = tmp_path / "products.csv"
    run(f"{site_url}/page1.html", "--no-browser", "--fields", "title,price", "--output", str(out))
    rows = list(csv.DictReader(out.open()))
    assert list(rows[0]) == ["title", "price"]
    assert rows[0]["price"] == "49.99"


def test_csv_columns_are_ordered_predictably(run, site_url, tmp_path):
    out = tmp_path / "products.csv"
    run(f"{site_url}/page1.html", "--no-browser", "--output", str(out))
    header = next(csv.reader(out.open()))
    assert header[:3] == ["title", "url", "price"]


def test_sqlite_output_is_directly_usable(run, site_url, tmp_path):
    out = tmp_path / "products.db"
    run(f"{site_url}/page1.html", "--no-browser", "--output", str(out))
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"] == 6
    row = conn.execute("SELECT data_json FROM records ORDER BY id LIMIT 1").fetchone()
    assert json.loads(row["data_json"])["title"] == "Alpha Keyboard"
    stats = conn.execute("SELECT stats_json, status FROM jobs").fetchone()
    assert stats["status"] == "completed"
    assert json.loads(stats["stats_json"])["records"] == 6
    conn.close()


def test_provenance_output_carries_signals(run, site_url, tmp_path):
    out = tmp_path / "products.json"
    run(f"{site_url}/page1.html", "--no-browser", "--provenance", "--output", str(out))
    rows = json.loads(out.read_text())
    assert rows[0]["price"]["source"] == "classname"
    assert rows[0]["price"]["confidence"] > 0.5
    assert rows[0]["price"]["signals"]


def test_url_file_processes_every_source(run, site_url, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text(f"# catalog\n{site_url}/page1.html\n\n{site_url}/page3.html\n")
    out = tmp_path / "products.jsonl"
    run(str(urls), "--no-browser", "--no-pagination", "--output", str(out))
    titles = [json.loads(line)["title"] for line in out.read_text().splitlines()]
    assert titles == ["Alpha Keyboard", "Beta Mouse", "Epsilon Headset", "Zeta Dock"]


def test_duplicates_across_sources_are_dropped(run, site_url, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site_url}/page1.html\n{site_url}/page1.html?x=1\n")
    out = tmp_path / "products.jsonl"
    run(str(urls), "--no-browser", "--no-pagination", "--output", str(out))
    assert len(out.read_text().strip().splitlines()) == 2


def test_a_failing_source_does_not_kill_the_job(run, site_url, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site_url}/missing.html\n{site_url}/page1.html\n")
    out = tmp_path / "products.jsonl"
    db = tmp_path / "job.db"
    result = run(
        str(urls), "--no-browser", "--no-pagination", "--output", str(out), "--db", str(db)
    )
    assert result.exit_code == 0
    assert len(out.read_text().strip().splitlines()) == 2
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT error_type FROM errors").fetchone()[0] == "HTTP_ERROR"
    conn.close()


def test_retry_recovers_a_source_that_works_the_second_time(run, site, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site.url}/later.html\n{site.url}/page1.html\n")
    db = tmp_path / "job.db"
    first = tmp_path / "first.jsonl"
    run(str(urls), "--no-browser", "--no-pagination", "--db", str(db), "--output", str(first))
    assert len(first.read_text().strip().splitlines()) == 2

    (site.root / "later.html").write_text(
        """<html><body><div class='product-grid'>
        <div class='product-card'><h2 class='product-title'><a href='/p/l1.html'>Late One</a></h2>
          <span class='price'>$5.00</span></div>
        <div class='product-card'><h2 class='product-title'><a href='/p/l2.html'>Late Two</a></h2>
          <span class='price'>$6.00</span></div>
        </div></body></html>"""
    )
    out = tmp_path / "retried.jsonl"
    result = run("retry", str(db), "--output", str(out))
    assert result.exit_code == 0
    titles = [json.loads(line)["title"] for line in out.read_text().splitlines()]
    assert titles == ["Late One", "Late Two"]


def test_retry_inherits_the_original_job_config(run, site, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site.url}/still-missing.html\n")
    db = tmp_path / "job.db"
    run(str(urls), "--no-browser", "--fields", "title", "--db", str(db))
    stored = json.loads(sqlite3.connect(db).execute("SELECT config_json FROM jobs").fetchone()[0])
    assert stored["acquirer"] == "http"
    assert list(stored["extraction"]["fields"]) == ["title"]
    # Retrying a permanently broken source yields nothing and says so.
    assert run("retry", str(db)).exit_code == 1


def test_a_successful_retry_clears_the_queue(run, site, tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text(f"{site.url}/arrives.html\n")
    db = tmp_path / "job.db"
    run(str(urls), "--no-browser", "--db", str(db))

    (site.root / "arrives.html").write_text(
        """<html><body><div class='grid'>
        <div class='card'><h2 class='title'>One</h2><span class='price'>$1.00</span></div>
        <div class='card'><h2 class='title'>Two</h2><span class='price'>$2.00</span></div>
        </div></body></html>"""
    )
    assert run("retry", str(db)).exit_code == 0
    result = run("retry", str(db))
    assert "nothing to retry" in (result.stdout + result.stderr)


def test_stdout_is_jsonl_when_no_output_given(run, site_url):
    result = run(f"{site_url}/page1.html", "--no-browser", "--no-pagination")
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(lines) == 2
    assert json.loads(lines[0])["title"] == "Alpha Keyboard"


def test_page_with_nothing_to_extract_is_not_a_crash(run, site_url, tmp_path):
    out = tmp_path / "empty.jsonl"
    result = run(f"{site_url}/empty.html", "--no-browser", "--output", str(out))
    assert result.exit_code == 0
    assert out.read_text().strip() == ""


def test_config_file_drives_the_whole_job(run, site_url, tmp_path):
    out = tmp_path / "from-config.csv"
    job = tmp_path / "job.jsonc"
    job.write_text(
        f"""{{
  // sources
  "sources": {{ "type": "url", "url": "{site_url}/page1.html" }},
  "acquirer": "http",
  "browser": {{ "enabled": false }},
  "pagination": {{ "max_pages": 2 }},
  "extraction": {{ "fields": {{ "title": "auto", "price": {{ "selector": ".price" }} }} }},
  "output": {{ "path": "{out}" }},
}}
"""
    )
    result = run(str(job))
    assert result.exit_code == 0
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 4
    assert rows[0]["price"] == "49.99"


def test_inspect_reports_structures_and_fields(run, site_url):
    result = run("inspect", f"{site_url}/page1.html", "--no-browser")
    output = result.stdout + result.stderr
    assert "STRUCTURES" in output
    assert "FIELDS" in output
    assert "PAGINATION" in output
    assert "product" in output


def test_inspect_schema_is_machine_readable(run, site_url):
    result = run("inspect", f"{site_url}/page1.html", "--no-browser", "--schema")
    schema = json.loads(result.stdout)
    assert schema["price"]["type"] == "number"
    assert 0 < schema["price"]["confidence"] <= 1


def test_inspect_explain_shows_the_signals(run, site_url):
    result = run("inspect", f"{site_url}/page1.html", "--no-browser", "--explain", "price")
    output = result.stdout + result.stderr
    assert "signals" in output
    assert "collection consistency" in output
