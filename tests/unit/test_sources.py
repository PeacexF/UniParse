from __future__ import annotations

import pytest

from uparse.config.schema import Config
from uparse.core.errors import SourceError
from uparse.sources.loader import normalize_url, read_url_file, resolve


def test_bare_host_gets_https():
    assert normalize_url("example.com/a") == "https://example.com/a"


def test_fragments_are_dropped():
    assert normalize_url("https://x.test/a#frag") == "https://x.test/a"


@pytest.mark.parametrize("bad", ["", "ftp://x.test/a", "   "])
def test_rejects_unusable_input(bad):
    with pytest.raises(SourceError):
        normalize_url(bad)


def test_url_file_skips_blanks_and_comments(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("# a comment\n\nhttps://x.test/a\n  https://x.test/b  \n\n# end\n")
    assert list(read_url_file(path)) == ["https://x.test/a", "https://x.test/b"]


def test_cli_target_wins_over_config():
    cfg = Config.model_validate({"sources": {"type": "url", "url": "https://config.test/"}})
    assert resolve(cfg, "https://cli.test/") == ["https://cli.test/"]


def test_duplicates_are_removed_in_order(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("https://x.test/b\nhttps://x.test/a\nhttps://x.test/b\n")
    assert resolve(Config(), str(path)) == ["https://x.test/b", "https://x.test/a"]


def test_local_html_file_becomes_a_file_url(tmp_path):
    page = tmp_path / "saved.html"
    page.write_text("<html></html>")
    assert resolve(Config(), str(page)) == [page.resolve().as_uri()]


def test_no_sources_is_an_error():
    with pytest.raises(SourceError):
        resolve(Config())
