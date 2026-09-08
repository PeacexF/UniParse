from __future__ import annotations

import csv
import json

import pytest

from uparse.config.schema import OutputConfig
from uparse.core.errors import ExportError
from uparse.exporters.writers import get_exporter

ROWS = [
    {"title": "A", "price": 1.5, "url": "https://x.test/a"},
    {"title": "B", "price": 2.0, "tags": ["x", "y"]},
]


def test_json_output_is_valid_and_pretty(tmp_path):
    out = tmp_path / "o.json"
    assert get_exporter("json").write(iter(ROWS), out) == 2
    assert json.loads(out.read_text()) == ROWS
    assert "\n" in out.read_text()


def test_json_compact_mode(tmp_path):
    out = tmp_path / "o.json"
    get_exporter("json", OutputConfig(pretty=False)).write(iter(ROWS), out)
    assert json.loads(out.read_text()) == ROWS


def test_empty_json_is_still_valid(tmp_path):
    out = tmp_path / "o.json"
    assert get_exporter("json").write(iter([]), out) == 0
    assert json.loads(out.read_text()) == []


def test_jsonl_is_one_object_per_line(tmp_path):
    out = tmp_path / "o.jsonl"
    get_exporter("jsonl").write(iter(ROWS), out)
    lines = out.read_text().strip().splitlines()
    assert [json.loads(line)["title"] for line in lines] == ["A", "B"]


def test_csv_serializes_nested_values_as_json(tmp_path):
    out = tmp_path / "o.csv"
    get_exporter("csv").write(iter(ROWS), out)
    rows = list(csv.DictReader(out.open()))
    assert rows[1]["tags"] == '["x", "y"]'


def test_csv_uses_the_union_of_all_keys(tmp_path):
    out = tmp_path / "o.csv"
    get_exporter("csv").write(iter(ROWS), out)
    assert set(next(csv.reader(out.open()))) == {"title", "price", "url", "tags"}


def test_csv_column_order_is_configurable(tmp_path):
    out = tmp_path / "o.csv"
    get_exporter("csv", OutputConfig(columns=["price", "title"])).write(iter(ROWS), out)
    assert next(csv.reader(out.open())) == ["price", "title"]


def test_unknown_format_is_rejected():
    with pytest.raises(ExportError):
        get_exporter("parquet")


def test_missing_directory_is_created(tmp_path):
    out = tmp_path / "deep" / "nested" / "o.jsonl"
    get_exporter("jsonl").write(iter(ROWS), out)
    assert out.exists()
