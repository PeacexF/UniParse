"""The regression corpus, run as tests.

`tools/eval.py` is the same checks with a scoreboard; this makes them fail CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from eval import CORPUS, evaluate

# `<slug>.intent.json` states an intent for the assisted path, which `make eval-assist`
# scores separately. It is not an expectation file.
SLUGS = sorted(p.stem for p in CORPUS.glob("*.json") if not p.name.endswith(".intent.json"))


@pytest.mark.parametrize("slug", SLUGS)
def test_corpus_page(slug):
    score = evaluate(slug)
    assert score.failures == [], f"{slug}: " + "; ".join(score.failures)


def test_the_corpus_is_not_empty():
    assert len(SLUGS) >= 10
