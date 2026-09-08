from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from uparse.config.schema import DedupeConfig
from uparse.core.models import Record

# Identity signals, strongest first. Title alone is never one of them.
IDENTITY_FIELDS = ("url", "sku", "email")


@dataclass(slots=True)
class Deduplicator:
    config: DedupeConfig = field(default_factory=DedupeConfig)
    _seen: set[str] = field(default_factory=set, repr=False)
    duplicates: int = 0

    def reset(self) -> None:
        self._seen.clear()

    def key_for(self, record: Record) -> str:
        if self.config.keys:
            values = [record.get(k) for k in self.config.keys]
            if any(v not in (None, "") for v in values):
                return _hash("keys", *(str(v) for v in values))
        for name in IDENTITY_FIELDS:
            value = record.get(name)
            if isinstance(value, str) and value.strip():
                return _hash(name, value.strip())
        return _hash("record", record.identity())

    def is_duplicate(self, record: Record) -> bool:
        if not self.config.enabled:
            return False
        key = self.key_for(record)
        if key in self._seen:
            self.duplicates += 1
            return True
        self._seen.add(key)
        return False

    def filter(self, records: Iterable[Record]) -> Iterator[Record]:
        for record in records:
            if not self.is_duplicate(record):
                yield record


def _hash(kind: str, *parts: str) -> str:
    blob = "\x00".join((kind, *parts))
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()
