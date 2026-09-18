"""Score the extraction engine against the saved corpus.

Every page in `tests/corpus/` is a real page with a hand-written expectation file. This
turns "did that change help?" into a number, so scoring weights can be moved on evidence
rather than on the last site someone looked at.

    make eval                                  # scoreboard
    make eval ARGS=-v                          # plus every mismatch
    make eval-assist ARGS="-c assist.jsonc"    # the LLM-assisted path, same pages

The assisted run is deliberately separate from `make check`: it needs a provider, costs
quota, and a free-tier model is not deterministic. It answers one question — does asking a
model produce fields the engine alone cannot name, and does it stay off the junk?
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
INTENT = ".intent.json"

Wanted = str | list[str]

NO_PROVIDER = '\n  No provider configured. The assisted eval needs one:\n    make eval-assist ARGS="-c assist.jsonc"\n  where assist.jsonc sets assist.provider, assist.model and the name of a key env var.\n'


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


def slugs() -> list[str]:
    return sorted(p.stem for p in CORPUS.glob("*.json") if not p.name.endswith(INTENT))


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


# ----------------------------------------------------------------- assisted


@dataclass(slots=True)
class Assisted:
    score: Score
    baseline: int = 0
    produced: int = 0
    recovered: int = 0
    gaps: int = 0
    provider: str = "-"
    error: str | None = None


def assist_slugs() -> list[str]:
    return sorted(p.name[: -len(INTENT)] for p in CORPUS.glob(f"*{INTENT}"))


def assist_evaluate(slug: str, config: Any, *, cache: bool) -> Assisted:
    """Generate a config for one corpus page, and score the fields it carries.

    The page comes off disk, so the only network call is the model's: this measures the
    assist layer and never the acquirer.
    """
    from uparse.assist import generate as run_generate

    spec, html = load(slug)
    intent = json.loads((CORPUS / f"{slug}{INTENT}").read_text())
    url = spec["url"]
    page = PageModel(url=url, final_url=url, html=html)

    expect = intent["expect"]
    floor = expect.get("coverage", 0.8)

    # The intent states the bar for this page, so it has to be the bar the gate uses too.
    # Generating at 0.8 and scoring at 0.75 measures two different things.
    settings = config.model_copy(
        update={"assist": config.assist.model_copy(update={"min_coverage": floor})}
    )

    generated = run_generate(page, settings, want=intent.get("want"), cache=cache)
    score = Score(name=slug)
    produced = set(generated.fields)

    # A page where the model never answered still carries the engine's own fields, and on an
    # easy page those pass every check. That would score the engine, not the assist.
    score.check(generated.error is None, f"assist did not run: {generated.error}")
    coverage = {a.name: a.coverage for a in generated.verdict.accepted}

    for wanted in expect.get("fields", []):
        hit = _resolve(wanted, produced)
        score.check(hit is not None, f"no field for {_label(wanted)}")
        if hit in coverage:
            score.check(
                coverage[hit] >= floor,
                f"{hit} resolves in {coverage[hit]:.0%} of records (< {floor:.0%})",
            )

    for name in expect.get("absent", []):
        score.check(name not in produced, f"{name!r} should not have been proposed")
    for junk in expect.get("absent_values", []):
        named = [a.name for a in generated.verdict.accepted if junk in a.sample]
        score.check(not named, f"{named} names the interface text {junk!r}")

    gaps = expect.get("gap", [])
    return Assisted(
        score=score,
        baseline=len(set(generated.baseline)),
        produced=len(produced),
        recovered=sum(1 for wanted in gaps if _resolve(wanted, produced) is not None),
        gaps=len(gaps),
        provider="cached" if generated.cached else generated.provider,
        error=generated.error,
    )


def _resolve(wanted: Wanted, produced: set[str]) -> str | None:
    """An intent may accept synonyms. A model that says `time` where the intent says
    `published` is not wrong, and pinning one exact string would score vocabulary rather
    than usefulness."""
    for name in [wanted] if isinstance(wanted, str) else wanted:
        if name in produced:
            return name
    return None


def _label(wanted: Wanted) -> str:
    return wanted if isinstance(wanted, str) else " / ".join(wanted)


def assist_main(args: argparse.Namespace) -> int:
    from uparse.config.loader import load_config
    from uparse.config.schema import Config

    config = load_config(args.config) if args.config else Config()
    if config.assist.provider == "null":
        print(NO_PROVIDER, file=sys.stderr)
        return 2

    pages = args.pages or assist_slugs()
    if not pages:
        print(f"\n  no {INTENT} files in {CORPUS}\n", file=sys.stderr)
        return 2

    runs = [assist_evaluate(slug, config, cache=not args.no_cache) for slug in pages]
    width = max(len(r.score.name) for r in runs)
    print(f"\n  provider: {runs[0].provider}\n")
    print(f"       {'page':<{width}}  {'checks':>10}  engine  config  gap")
    for run in runs:
        score = run.score
        mark = "ok  " if score.ratio == 1.0 else "FAIL"
        gap = f"{run.recovered}/{run.gaps}" if run.gaps else "-"
        print(
            f"  {mark} {score.name:<{width}}  {score.passed:>4}/{score.checks:<5}"
            f"  {run.baseline:>6}  {run.produced:>6}  {gap:>4}"
        )
        if run.error:
            print(f"       - assist unavailable: {run.error}")
        if args.verbose or score.ratio < 1.0:
            for failure in score.failures:
                print(f"       - {failure}")

    checks = sum(r.score.checks for r in runs)
    passed = sum(r.score.passed for r in runs)
    recovered = sum(r.recovered for r in runs)
    gaps = sum(r.gaps for r in runs)
    print(
        f"\n  {passed}/{checks} checks ({passed / checks:.0%}) · "
        f"{recovered}/{gaps} fields recovered that the engine alone does not name\n"
    )
    return 0 if passed == checks else 1


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
    parser.add_argument(
        "--assist", action="store_true", help="score the LLM-assisted path (needs a provider)"
    )
    parser.add_argument("-c", "--config", help="JSONC config carrying the `assist` block")
    parser.add_argument("--no-cache", action="store_true", help="ignore cached proposals")
    parser.add_argument("pages", nargs="*", help="limit to these corpus slugs")
    args = parser.parse_args()

    if args.assist:
        return assist_main(args)

    pages = args.pages or slugs()
    scores = [evaluate(slug) for slug in pages]

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
