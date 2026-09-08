from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from uparse.config.schema import Config, SourcesConfig
from uparse.core.errors import SourceError

URL_SCHEMES = {"http", "https", "file"}


def looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in URL_SCHEMES and bool(parsed.netloc or parsed.path)


def normalize_url(value: str) -> str:
    value = value.strip()
    if not value:
        raise SourceError("empty URL")
    parsed = urlparse(value)
    if not parsed.scheme:
        parsed = urlparse(f"https://{value}")
    if parsed.scheme not in URL_SCHEMES:
        raise SourceError(f"unsupported URL scheme: {parsed.scheme!r}")
    if parsed.scheme != "file" and not parsed.netloc:
        raise SourceError(f"malformed URL: {value!r}")
    path = parsed.path or ("/" if parsed.scheme != "file" else "")
    return urlunparse(parsed._replace(path=path, fragment=""))


def read_url_file(path: str | Path) -> Iterator[str]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(f"cannot read {p}: {exc}") from exc
    yield from _parse_lines(text)


def read_stdin() -> Iterator[str]:
    if sys.stdin.isatty():
        raise SourceError("no data on stdin")
    yield from _parse_lines(sys.stdin.read())


def _parse_lines(text: str) -> Iterator[str]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        yield normalize_url(line)


def resolve(config: Config, target: str | None = None) -> list[str]:
    """CLI target wins over config.sources; both are deduplicated in order."""
    urls = list(_from_target(target)) if target else list(_from_config(config.sources))
    if not urls:
        raise SourceError(
            "no sources: pass a URL, a file of URLs, '-' for stdin, or set sources in config"
        )
    return _dedupe(urls)


def _from_target(target: str) -> Iterator[str]:
    if target == "-":
        yield from read_stdin()
        return
    path = Path(target)
    if path.exists() and path.is_file():
        if path.suffix.lower() in {".html", ".htm"}:
            yield path.resolve().as_uri()
            return
        yield from read_url_file(path)
        return
    yield normalize_url(target)


def _from_config(sources: SourcesConfig) -> Iterator[str]:
    match sources.type:
        case "stdin":
            yield from read_stdin()
        case "file":
            if sources.path is None:
                raise SourceError("sources.path is required for type 'file'")
            yield from read_url_file(sources.path)
        case _:
            if sources.url:
                yield normalize_url(sources.url)
            for u in sources.urls:
                yield normalize_url(u)


def _dedupe(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
