from __future__ import annotations

import logging
import os
import sys
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

THEME = Theme(
    {
        "ok": "bold green",
        "fail": "bold red",
        "warn": "yellow",
        "muted": "dim",
        "field": "cyan",
        "score": "magenta",
        "url": "blue underline",
    }
)

# stdout is reserved for piped data; all human output goes to stderr.
console = Console(theme=THEME, stderr=True, highlight=False)
out_console = Console(highlight=False, soft_wrap=True)

_LEVELS = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}


def setup_logging(verbosity: int = 0, *, debug: bool = False) -> None:
    level = logging.DEBUG if debug else _LEVELS.get(min(verbosity, 2), logging.DEBUG)
    handler = RichHandler(
        console=console,
        show_time=debug,
        show_path=debug,
        rich_tracebacks=debug,
        markup=True,
        omit_repeated_times=False,
    )
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[handler],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("uparse") else f"uparse.{name}")


def is_debug() -> bool:
    return logging.getLogger("uparse").isEnabledFor(logging.DEBUG)


def progress_line(index: int, total: int, url: str, ok: bool, detail: str) -> None:
    marker = "[ok]✓[/ok]" if ok else "[fail]✗[/fail]"
    width = len(str(total))
    console.print(f"[muted][{index:>{width}}/{total}][/muted] {_short(url)} {marker} {detail}")


def _short(url: str, limit: int = 64) -> str:
    trimmed = url.removeprefix("https://").removeprefix("http://")
    return trimmed if len(trimmed) <= limit else trimmed[: limit - 1] + "…"


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def die(message: str, *, code: int = 1) -> Any:
    console.print(f"[fail]error[/fail] {message}")
    sys.exit(code)
