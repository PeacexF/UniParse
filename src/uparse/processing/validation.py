from __future__ import annotations

from dataclasses import dataclass, field

from uparse.config.schema import ExtractionConfig
from uparse.core.models import Record, ValueType

MIN_USEFUL_FIELDS = 1


@dataclass(slots=True, frozen=True)
class Problem:
    record_index: int
    field: str
    reason: str


@dataclass(slots=True)
class ValidationReport:
    kept: list[Record] = field(default_factory=list)
    dropped: int = 0
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def validate(records: list[Record], config: ExtractionConfig) -> ValidationReport:
    """Drop unusable records, report the rest. Never raises: a bad record is data, not a crash."""
    report = ValidationReport()
    required = [name for name, spec in config.fields.items() if spec.required]
    for record in records:
        problems = _problems(record, required)
        if _is_empty(record):
            report.dropped += 1
            report.problems.append(Problem(record.index, "*", "no usable fields"))
            continue
        missing = [p for p in problems if p.reason == "required field missing"]
        if missing:
            report.dropped += 1
            report.problems.extend(problems)
            continue
        report.problems.extend(problems)
        report.kept.append(record)
    return report


def _problems(record: Record, required: list[str]) -> list[Problem]:
    out: list[Problem] = []
    for name in required:
        if record.get(name) in (None, "", []):
            out.append(Problem(record.index, name, "required field missing"))
    for name, fv in record.fields.items():
        if (
            fv.type is ValueType.URL
            and isinstance(fv.value, str)
            and not fv.value.startswith(("http", "file"))
        ):
            out.append(Problem(record.index, name, "URL is not absolute"))
    return out


def _is_empty(record: Record) -> bool:
    usable = [
        fv
        for fv in record.fields.values()
        if fv.value not in (None, "", [], {}) and str(fv.value).strip()
    ]
    return len(usable) < MIN_USEFUL_FIELDS
