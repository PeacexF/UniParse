"""Test every suggestion against the page it came from before it reaches a config file.

A proposal is a hypothesis. The model is often a small free-tier one, and a wrong-but-
plausible name is its most likely failure — so nothing is written until the selector behind
it has been re-run against the real records and produced real values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field

from lxml.html import HtmlElement

from uparse.assist.brief import ColumnBrief, PageBrief
from uparse.assist.protocol import Proposal
from uparse.core.models import Record
from uparse.extraction.htmlutil import absolutize, image_url, node_text, select_within

NAME = re.compile(r"[a-z][a-z0-9_]{1,39}")
SEPARATORS = re.compile(r"[\s\-./]+")
UNUSABLE = re.compile(r"[^a-z0-9_]")


def slug(name: str) -> str:
    """`"stars today"` -> `stars_today`, and anything less tidy than that -> `""`.

    A model asked for lower_snake_case often answers in the words the user typed, and
    losing a correct column mapping to a space would be silly. Separators are the only
    thing rewritten: a name carrying anything else comes back empty and is rejected,
    because a guessed-at field name costs more than a missing one.
    """
    flat = re.sub(r"_{2,}", "_", SEPARATORS.sub("_", name.strip().lower())).strip("_")
    return "" if UNUSABLE.search(flat) else flat


@dataclass(slots=True)
class Accepted:
    name: str
    selector: str
    column: str
    coverage: float
    why: str
    sample: str
    engine_name: str | None = None

    @property
    def agrees_with_engine(self) -> bool:
        """The model confirmed a name the engine already produces. Nothing to pin."""
        return self.engine_name == self.name


@dataclass(slots=True)
class Rejected:
    name: str
    column: str
    reason: str


@dataclass(slots=True)
class Ignored:
    """A column the model called interface text. Recorded, so the choice is reviewable."""

    column: str
    sample: str


@dataclass(slots=True)
class Verdict:
    accepted: list[Accepted]
    rejected: list[Rejected]
    ignored: list[Ignored] = dc_field(default_factory=list)
    collection: str | None = None
    notes: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.accepted)


def check(
    proposal: Proposal,
    brief: PageBrief,
    records: list[Record],
    *,
    min_coverage: float = 0.8,
    max_fields: int = 24,
) -> Verdict:
    accepted: list[Accepted] = []
    rejected: list[Rejected] = []
    claimed: set[str] = set()

    for item in proposal.fields:
        if len(accepted) >= max_fields:
            rejected.append(Rejected(item.name, item.column, "field limit reached"))
            continue
        name = slug(item.name)
        if not NAME.fullmatch(name):
            rejected.append(Rejected(item.name, item.column, "not a usable field name"))
            continue
        if name in claimed:
            rejected.append(Rejected(name, item.column, "duplicate name"))
            continue
        column = brief.column(item.column)
        if column is None:
            rejected.append(Rejected(name, item.column, "no such column in the report"))
            continue

        coverage, sample = _measure(column, records)
        if coverage < min_coverage:
            rejected.append(Rejected(name, item.column, f"resolves in {coverage:.0%} of records"))
            continue

        claimed.add(name)
        accepted.append(
            Accepted(
                name=name,
                selector=column.selector,
                column=item.column,
                coverage=coverage,
                why=item.why,
                sample=sample,
                engine_name=column.named,
            )
        )

    return Verdict(
        accepted=accepted,
        rejected=rejected,
        ignored=_ignored(proposal, brief, claimed={a.column for a in accepted}),
        collection=_collection(proposal, brief),
        notes=proposal.notes,
    )


def _ignored(proposal: Proposal, brief: PageBrief, *, claimed: set[str]) -> list[Ignored]:
    """Only ids that exist. A drop the model also proposed as a field is not a drop."""
    out: list[Ignored] = []
    for column_id in dict.fromkeys(proposal.drop):
        column = brief.column(column_id)
        if column is None or column_id in claimed:
            continue
        out.append(Ignored(column_id, next(iter(column.samples), "")))
    return out


def _collection(proposal: Proposal, brief: PageBrief) -> str | None:
    if proposal.collection is None:
        return None
    found = brief.collection(proposal.collection)
    return found.selector if found is not None else None


def _measure(column: ColumnBrief, records: list[Record]) -> tuple[float, str]:
    """Measure what the config will actually do.

    A column the engine already names is emitted as that field under a new name, so what
    matters is the value the engine produced — the un-clipped title, the absolutized URL —
    and not what re-running a selector would read back. Only an unnamed column is emitted
    as a selector, and that one is re-run inside every record: the whole safety story.
    """
    if column.named:
        return _from_extracted(column.named, records)
    selector = column.selector
    if not records or selector.startswith("(field:"):
        return _from_extracted(selector.removeprefix("(field:").rstrip(")"), records)
    hits = 0
    sample = ""
    for record in records:
        node = record.node
        if node is None:
            continue
        found = select_within(node, selector)
        value = _value(found[0], record.page_url) if found else ""
        if value:
            hits += 1
            sample = sample or value[:80]
    return (hits / len(records) if records else 0.0), sample


def _value(node: HtmlElement, base_url: str) -> str:
    """Text, or the URL an element carries. An <img> has no text but is not empty."""
    text = node_text(node, limit=400)
    if text:
        return text
    if node.tag == "img":
        return image_url(node, base_url)
    return absolutize(base_url, node.get("href") or node.get("src") or node.get("content"))


def _from_extracted(name: str, records: list[Record]) -> tuple[float, str]:
    """A field the engine produced is judged on its values, which are the deliverable."""
    values = [r.get(name) for r in records]
    present = [v for v in values if v not in (None, "")]
    coverage = len(present) / len(values) if values else 0.0
    return coverage, str(present[0])[:80] if present else ""
