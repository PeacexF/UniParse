from __future__ import annotations

from dataclasses import dataclass, field

from uparse.config.schema import Config
from uparse.core.models import PageModel, PaginationHint, Record
from uparse.extraction.engine import ExtractionResult, extract
from uparse.navigation.pagination import find_next
from uparse.processing.validation import ValidationReport, validate


@dataclass(slots=True)
class PageOutcome:
    page: PageModel
    records: list[Record] = field(default_factory=list)
    result: ExtractionResult | None = None
    report: ValidationReport | None = None
    next_hint: PaginationHint | None = None


def process(page: PageModel, config: Config, *, seen_urls: set[str] | None = None) -> PageOutcome:
    """One page in, records plus a next-page hint out. No I/O, no state."""
    result = extract(page, config.extraction)
    report = validate(result.records, config.extraction)
    hint = None
    if result.root is not None:
        hint = find_next(result.root, page, config.pagination, seen=seen_urls or set())
    return PageOutcome(page=page, records=report.kept, result=result, report=report, next_hint=hint)
