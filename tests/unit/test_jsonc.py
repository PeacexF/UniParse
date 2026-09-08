from __future__ import annotations

import pytest

from uparse.config.jsonc import loads, strip_jsonc
from uparse.core.errors import ConfigError


def test_line_and_block_comments_are_removed():
    text = """
    {
      // a line comment
      "a": 1, /* inline */
      "b": "keep // this",   // trailing
      /* multi
         line */
      "c": [1, 2, 3,],
    }
    """
    assert loads(text) == {"a": 1, "b": "keep // this", "c": [1, 2, 3]}


def test_urls_inside_strings_survive():
    assert loads('{"u": "https://example.com/a//b"}') == {"u": "https://example.com/a//b"}


def test_escaped_quote_does_not_end_the_string():
    assert loads(r'{"a": "say \"hi\" // not a comment"}') == {"a": 'say "hi" // not a comment'}


def test_stripping_preserves_line_numbers():
    text = '{\n// comment\n"a": 1\n}'
    assert strip_jsonc(text).count("\n") == text.count("\n")


def test_bad_json_reports_position():
    with pytest.raises(ConfigError) as excinfo:
        loads('{"a": }', origin="job.jsonc")
    assert "job.jsonc:1:" in str(excinfo.value)
