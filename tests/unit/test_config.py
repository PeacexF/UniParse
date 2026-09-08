from __future__ import annotations

import pytest

from uparse.config.loader import build_config, load_config
from uparse.config.schema import Config
from uparse.core.errors import ConfigError


def test_defaults_are_conservative():
    cfg = Config()
    assert cfg.browser.concurrency == 4
    assert cfg.browser.headless is True
    assert cfg.pagination.max_pages == 25
    assert cfg.extraction.mode == "auto"


@pytest.mark.parametrize(
    ("spec", "mode", "selector"),
    [
        ("auto", "auto", None),
        (".product-title", "selector", ".product-title"),
        ({"selector": "[data-price]"}, "selector", "[data-price]"),
        ({"selector": "img", "attribute": "src"}, "attribute", "img"),
        ({"regex": r"\d+"}, "regex", None),
    ],
)
def test_field_spec_shorthand(spec, mode, selector):
    cfg = build_config({"extraction": {"fields": {"x": spec}}})
    assert cfg.extraction.fields["x"].mode == mode
    assert cfg.extraction.fields["x"].selector == selector


@pytest.mark.parametrize(
    ("path", "fmt"),
    [("out.json", "json"), ("out.jsonl", "jsonl"), ("out.csv", "csv"), ("out.db", "sqlite")],
)
def test_output_format_inferred_from_extension(path, fmt):
    assert build_config({"output": {"path": path}}).output.format == fmt


def test_unknown_key_is_rejected():
    with pytest.raises(ConfigError):
        build_config({"browser": {"nope": 1}})


def test_unknown_resource_type_is_rejected():
    with pytest.raises(ConfigError) as excinfo:
        build_config({"browser": {"block_resources": ["font", "banana"]}})
    assert "banana" in str(excinfo.value)


def test_merged_overrides_are_deep():
    cfg = Config().merged(browser={"concurrency": 9}, pagination={"max_pages": 3})
    assert cfg.browser.concurrency == 9
    assert cfg.browser.headless is True
    assert cfg.pagination.max_pages == 3


def test_load_config_from_file(tmp_path):
    path = tmp_path / "job.jsonc"
    path.write_text('{\n  // comment\n  "browser": {"concurrency": 2},\n}\n')
    assert load_config(path).browser.concurrency == 2
