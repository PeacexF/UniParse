"""What a provider is, and what it is allowed to say back.

Providers differ enormously in what they support — a free Flash tier and a 7B model behind
`ollama run` are not the same thing — so a provider declares a `Tier` and the caller asks
in the strongest way that provider understands, degrading rather than failing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from uparse.core.errors import UparseError


class AssistError(UparseError):
    """The assist layer failed. Never fatal: generation falls back to the engine alone."""


class Tier(StrEnum):
    """How precisely a provider can be asked for structured output."""

    SCHEMA = "schema"  # native JSON-schema response format
    JSON = "json"  # JSON mode, no schema
    TEXT = "text"  # plain text; we ask for JSON and parse defensively


@dataclass(slots=True)
class FieldProposal:
    """One field the model wants in the config, citing a column id from the brief."""

    name: str
    column: str
    why: str = ""


@dataclass(slots=True)
class Proposal:
    fields: list[FieldProposal] = field(default_factory=list)
    collection: str | None = None
    drop: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Proposal:
        fields = _fields(payload) or _flat_fields(payload)
        collection = payload.get("collection")
        return cls(
            fields=fields,
            collection=collection.strip() if isinstance(collection, str) and collection else None,
            drop=[d for d in (payload.get("drop") or []) if isinstance(d, str)],
            notes=str(payload.get("notes") or "").strip(),
        )


ID = re.compile(r"[cg][0-9]{1,3}")


def _fields(payload: dict[str, Any]) -> list[FieldProposal]:
    out: list[FieldProposal] = []
    for raw in payload.get("fields") or []:
        if not isinstance(raw, dict):
            continue
        name, column = raw.get("name"), raw.get("column")
        if isinstance(name, str) and isinstance(column, str) and name and column:
            out.append(
                FieldProposal(
                    name=name.strip(),
                    column=column.strip(),
                    why=str(raw.get("why") or "").strip(),
                )
            )
    return out


ID_KEYS = ("column", "column_id", "id", "col")


def _flat_fields(payload: dict[str, Any]) -> list[FieldProposal]:
    """`{"title": "c4"}` and `{"title": {"column_id": "c4"}}` — the shapes small models
    reach for whatever the schema says.

    Accepting them costs nothing: every id still has to exist in the brief and every
    selector still has to survive the gate, so a wrong guess fails closed exactly as
    before. Refusing them costs a page.
    """
    out: list[FieldProposal] = []
    for name, value in payload.items():
        column = _id_in(value)
        if column:
            out.append(FieldProposal(name=name.strip(), column=column))
    return out


def _id_in(value: Any) -> str:
    if isinstance(value, dict):
        value = next((value[k] for k in ID_KEYS if isinstance(value.get(k), str)), None)
    if isinstance(value, str) and ID.fullmatch(value.strip()):
        return value.strip()
    return ""


@runtime_checkable
class Provider(Protocol):
    name: str
    tier: Tier

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        """Return the model's raw answer. Parsing and validation are the caller's job."""
        ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_payload(text: str) -> dict[str, Any]:
    """Pull a JSON object out of whatever a model actually said.

    Tier TEXT providers wrap JSON in prose, in fences, or in both. Being liberal here is
    what lets a local model participate at all; the validation gate is what keeps that safe.
    """
    for candidate in _candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssistError("no JSON object in the model's answer")


def _candidates(text: str) -> list[str]:
    out: list[str] = [text.strip()]
    fenced = _FENCE.search(text)
    if fenced:
        out.append(fenced.group(1).strip())
    balanced = _balanced_object(text)
    if balanced:
        out.append(balanced)
    return [c for c in out if c]


def _balanced_object(text: str) -> str:
    """The first {...} that closes, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""
