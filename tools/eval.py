"""Score the extraction engine against the saved corpus.

Every page in `tests/corpus/` is a real page with a hand-written expectation file. This
turns "did that change help?" into a number, so scoring weights can be moved on evidence
rather than on the last site someone looked at.

    make eval            # scoreboard
    make eval ARGS=-v    # plus every mismatch
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from uparse.core.models import PageModel
from uparse.extraction.engine import ExtractionResult, extract

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "corpus"


@dataclass(slots=True)
class Score:
    name: str
    checks: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    def check(self, ok: bool, detail: str) -> None:
        self.checks += 1
        if ok:
            self.passed += 1
        else:
            self.failures.append(detail)

    @property
    def ratio(self) -> float:
        return self.passed / self.checks if self.checks else 0.0


def load(slug: str) -> tuple[dict[str, Any], str]:
    spec = json.loads((CORPUS / f"{slug}.json").read_text())
    html = gzip.decompress((CORPUS / f"{slug}.html.gz").read_bytes()).decode("utf-8")
    return spec, html


def evaluate(slug: str) -> Score:
    spec, html = load(slug)
    url = spec["url"]
    result = extract(PageModel(url=url, final_url=url, html=html))
    score = Score(name=slug)
    expect = spec["expect"]

    if "strategy" in expect:
        score.check(
            result.strategy == expect["strategy"],
            f"strategy {result.strategy!r} != {expect['strategy']!r}",
        )
    if "records" in expect:
        score.check(
            len(result.records) == expect["records"],
            f"records {len(result.records)} != {expect['records']}",
        )
    if "min_records" in expect:
        score.check(
            len(result.records) >= expect["min_records"],
            f"records {len(result.records)} < {expect['min_records']}",
        )

    _check_fields(score, result, expect)
    _check_absent(score, result, expect)
    return score


def _check_fields(score: Score, result: ExtractionResult, expect: dict[str, Any]) -> None:
    first = result.records[0].to_dict() if result.records else {}
    for name, wanted in expect.get("first_record", {}).items():
        actual = first.get(name)
        score.check(_matches(actual, wanted), f"first.{name} = {actual!r}, wanted {wanted!r}")

    for name in expect.get("required_fields", []):
        present = sum(1 for r in result.records if r.get(name) not in (None, ""))
        coverage = present / len(result.records) if result.records else 0.0
        floor = expect.get("coverage", 0.9)
        score.check(
            coverage >= floor, f"{name} present in {coverage:.0%} of records (< {floor:.0%})"
        )


def _check_absent(score: Score, result: ExtractionResult, expect: dict[str, Any]) -> None:
    names = {name for record in result.records for name in record.fields}
    for name in expect.get("absent_fields", []):
        score.check(name not in names, f"{name!r} should not be extracted")


def _matches(actual: Any, wanted: Any) -> bool:
    if isinstance(wanted, dict) and "contains" in wanted:
        return wanted["contains"] in str(actual)
    if isinstance(wanted, dict) and "startswith" in wanted:
        return str(actual).startswith(wanted["startswith"])
    if isinstance(wanted, float) and isinstance(actual, int | float):
        return abs(actual - wanted) < 0.005
    return bool(actual == wanted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="list every mismatch")
    parser.add_argument("pages", nargs="*", help="limit to these corpus slugs")
    args = parser.parse_args()

    slugs = args.pages or sorted(p.stem for p in CORPUS.glob("*.json"))
    scores = [evaluate(slug) for slug in slugs]

    checks = sum(s.checks for s in scores)
    passed = sum(s.passed for s in scores)
    width = max(len(s.name) for s in scores)
    print()
    for s in scores:
        mark = "ok  " if s.ratio == 1.0 else "FAIL"
        print(f"  {mark} {s.name:<{width}}  {s.passed:>2}/{s.checks:<2} checks")
        if args.verbose or s.ratio < 1.0:
            for failure in s.failures:
                print(f"       - {failure}")
    perfect = sum(1 for s in scores if s.ratio == 1.0)
    print(
        f"\n  {passed}/{checks} checks ({passed / checks:.0%}) · {perfect}/{len(scores)} pages clean\n"
    )
    return 0 if passed == checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
