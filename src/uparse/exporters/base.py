from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, TextIO

from uparse.core.errors import ExportError


class Exporter(Protocol):
    extension: str

    def write(self, rows: Iterable[dict[str, Any]], target: Path | None) -> int: ...


@contextmanager
def open_target(target: Path | None) -> Iterator[TextIO]:
    """A None target streams to stdout, which is what makes `uparse … | jq` work."""
    if target is None:
        yield sys.stdout
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = target.open("w", encoding="utf-8", newline="")
    except OSError as exc:
        raise ExportError(f"cannot write {target}: {exc}") from exc
    try:
        yield handle
    finally:
        handle.close()


def collect_columns(
    rows: Iterable[dict[str, Any]], preferred: list[str] | None = None
) -> list[str]:
    """Stable column order: configured first, then first-seen order."""
    seen: list[str] = list(preferred or [])
    known = set(seen)
    for row in rows:
        for key in row:
            if key not in known:
                known.add(key)
                seen.append(key)
    return seen
