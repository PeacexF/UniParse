from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uparse.core.errors import ConfigError

_WS = " \t\r\n"


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, preserving string literals.

    Comment bodies are replaced by spaces of equal length so that json error
    offsets still point at the right place in the original text.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i)
                j = n if j == -1 else j
                out.append(" " * (j - i))
                i = j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                j = n if j == -1 else j + 2
                out.append("".join(c if c == "\n" else " " for c in text[i:j]))
                i = j
                continue
        out.append(ch)
        i += 1
    return _strip_trailing_commas("".join(out))


def _strip_trailing_commas(text: str) -> str:
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in _WS:
                j += 1
            if j < n and text[j] in "]}":
                out[i] = " "
        i += 1
    return "".join(out)


def loads(text: str, *, origin: str = "<string>") -> Any:
    try:
        return json.loads(strip_jsonc(text))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{origin}:{exc.lineno}:{exc.colno}: {exc.msg}") from exc


def load(path: str | Path) -> Any:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {p}: {exc}") from exc
    return loads(text, origin=str(p))
