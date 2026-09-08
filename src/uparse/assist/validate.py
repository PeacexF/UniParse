"""Test every suggestion against the page it came from before it reaches a config file.

A proposal is a hypothesis. The model is often a small free-tier one, and a wrong-but-
plausible name is its most likely failure — so nothing is written until the selector behind
it has been re-run against the real records and produced real values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml.html import HtmlElement

from uparse.assist.brief import PageBrief
from uparse.assist.protocol import Proposal
from uparse.core.models import Record
from uparse.extraction.htmlutil import absolutize, image_url, node_text, select_within

NAME = re.compile(r"[a-z][a-z0-9_]{1,39}")


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
class Verdict:
    accepted: list[Accepted]
    rejected: list[Rejected]
    collection: str | None = None

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
        name = item.name.strip().lower()
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

        coverage, sample = _measure(column.selector, records)
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
        collection=_collection(proposal, brief),
    )


def _collection(proposal: Proposal, brief: PageBrief) -> str | None:
    if proposal.collection is None:
        return None
    found = brief.collection(proposal.collection)
    return found.selector if found is not None else None


def _measure(selector: str, records: list[Record]) -> tuple[float, str]:
    """Re-run a selector inside each record. This is the whole safety story."""
    if not records or selector.startswith("(field:"):
        return _from_extracted(selector, records)
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


def _from_extracted(selector: str, records: list[Record]) -> tuple[float, str]:
    """Fields with no re-queryable selector (structured data) are judged on their values."""
    name = selector.removeprefix("(field:").rstrip(")")
    values = [r.get(name) for r in records]
    present = [v for v in values if v not in (None, "")]
    coverage = len(present) / len(values) if values else 0.0
    return coverage, str(present[0])[:80] if present else ""
