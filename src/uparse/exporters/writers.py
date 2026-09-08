from __future__ import annotations

import csv
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from uparse.config.schema import OutputConfig
from uparse.core.errors import ExportError
from uparse.exporters.base import collect_columns, open_target

PREFERRED_ORDER = ["title", "url", "price", "currency", "image", "description", "date"]


class JsonExporter:
    extension = "json"

    def __init__(self, config: OutputConfig | None = None) -> None:
        self.config = config or OutputConfig()

    def write(self, rows: Iterable[dict[str, Any]], target: Path | None) -> int:
        indent = 2 if self.config.pretty else None
        count = 0
        with open_target(target) as out:
            out.write("[\n" if indent else "[")
            for row in rows:
                if count:
                    out.write(",\n" if indent else ",")
                chunk = json.dumps(row, ensure_ascii=False, indent=indent, default=str)
                out.write(_reindent(chunk, indent) if indent else chunk)
                count += 1
            out.write("\n]\n" if indent else "]\n")
        return count


def _reindent(chunk: str, indent: int) -> str:
    pad = " " * indent
    return pad + chunk.replace("\n", "\n" + pad)


class JsonlExporter:
    extension = "jsonl"

    def __init__(self, config: OutputConfig | None = None) -> None:
        self.config = config or OutputConfig()

    def write(self, rows: Iterable[dict[str, Any]], target: Path | None) -> int:
        count = 0
        with open_target(target) as out:
            for row in rows:
                out.write(json.dumps(row, ensure_ascii=False, default=str))
                out.write("\n")
                count += 1
        return count


class CsvExporter:
    """Scalars become columns; nested values become JSON strings. Nothing cleverer."""

    extension = "csv"

    def __init__(self, config: OutputConfig | None = None) -> None:
        self.config = config or OutputConfig()

    def write(self, rows: Iterable[dict[str, Any]], target: Path | None) -> int:
        # CSV needs the full header up front, so this is the one format that buffers.
        materialized = list(rows)
        columns = self.config.columns or collect_columns(materialized, _preferred(materialized))
        count = 0
        with open_target(target) as out:
            writer = csv.DictWriter(
                out,
                fieldnames=columns,
                delimiter=self.config.csv_delimiter,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in materialized:
                writer.writerow({k: _flatten(v) for k, v in row.items()})
                count += 1
        return count


def _preferred(rows: list[dict[str, Any]]) -> list[str]:
    present = {key for row in rows for key in row}
    return [name for name in PREFERRED_ORDER if name in present]


def _flatten(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


AnyExporter = JsonExporter | JsonlExporter | CsvExporter
EXPORTERS: dict[str, Callable[[OutputConfig | None], AnyExporter]] = {
    "json": JsonExporter,
    "jsonl": JsonlExporter,
    "csv": CsvExporter,
}


def get_exporter(fmt: str, config: OutputConfig | None = None) -> AnyExporter:
    try:
        return EXPORTERS[fmt](config)
    except KeyError:
        raise ExportError(
            f"unknown output format {fmt!r}; expected one of {', '.join(EXPORTERS)}"
        ) from None
