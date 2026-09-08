from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from uparse.core.models import SOURCE_RANK, Candidate, Signal, Source

# Starting confidence contributed by the extraction strategy itself.
SOURCE_BASE: dict[Source, float] = {
    Source.CONFIG: 1.00,
    Source.JSONLD: 0.55,
    Source.MICRODATA: 0.50,
    Source.OPENGRAPH: 0.45,
    Source.EMBEDDED_JSON: 0.40,
    Source.TABLE: 0.40,
    Source.CLASSNAME: 0.32,
    Source.ATTRIBUTE: 0.30,
    Source.SEMANTIC: 0.30,
    Source.PATTERN: 0.28,
    Source.DOM: 0.25,
    Source.POSITION: 0.12,
}

BONUS = {
    "name_exact": 0.15,
    "name_partial": 0.08,
    "type_agrees": 0.15,
    "pattern_agrees": 0.12,
    "consistent": 0.16,
    "unique_in_record": 0.05,
    "semantic_tag": 0.10,
}
PENALTY = {
    "too_long": -0.12,
    "too_short": -0.08,
    "inconsistent": -0.12,
    "boilerplate": -0.15,
}


def base_signal(source: Source, detail: str = "") -> Signal:
    return Signal(f"source:{source}", SOURCE_BASE.get(source, 0.2), detail)


def score(candidate: Candidate) -> float:
    total = sum(s.weight for s in candidate.signals)
    candidate.confidence = round(max(0.0, min(total, 1.0)), 4)
    return candidate.confidence


def score_all(candidates: Iterable[Candidate]) -> list[Candidate]:
    out = list(candidates)
    for c in out:
        score(c)
    return out


def best_per_field(candidates: Iterable[Candidate]) -> dict[str, Candidate]:
    """Highest confidence wins; ties break on strategy priority, then on value length."""
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        grouped[c.name].append(c)
    return {
        name: max(group, key=lambda c: (c.confidence, -SOURCE_RANK[c.source], _len(c.value)))
        for name, group in grouped.items()
    }


def _len(value: object) -> int:
    try:
        return len(value)  # type: ignore[arg-type]
    except TypeError:
        return 0


def apply_consistency(records: list[dict[str, list[Candidate]]]) -> None:
    """Reward selectors that produced the same field across most records in a collection.

    Consistency is what separates a real column from a lucky one-off match, so it is
    applied after all records in a collection have been extracted.
    """
    if len(records) < 2:
        return
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for record in records:
        for name, cands in record.items():
            for selector in {c.selector for c in cands if c.selector}:
                counts[(name, str(selector))] += 1
    total = len(records)
    for record in records:
        for name, cands in record.items():
            for c in cands:
                if not c.selector:
                    continue
                ratio = counts[(name, c.selector)] / total
                if ratio >= 0.8:
                    c.signals.append(
                        Signal(
                            "collection consistency", BONUS["consistent"], f"{ratio:.0%} of items"
                        )
                    )
                elif ratio < 0.35:
                    c.signals.append(
                        Signal(
                            "inconsistent across items",
                            PENALTY["inconsistent"],
                            f"{ratio:.0%} of items",
                        )
                    )
                score(c)


def explain(candidate: Candidate) -> str:
    return candidate.explain()
