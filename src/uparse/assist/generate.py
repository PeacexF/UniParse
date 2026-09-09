"""Turn a page plus an intent into a reviewable job file.

    acquire → extract → brief → propose → validate → job.jsonc → a human → run

The model's part is the third arrow only. Everything the generated config does at run time
is deterministic, and the file itself is the deliverable: readable, diffable, and free of
any dependency on the model that suggested it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uparse.assist import prompt as prompts
from uparse.assist.brief import PageBrief
from uparse.assist.brief import build as build_brief
from uparse.assist.protocol import AssistError, Proposal, Provider, Tier, parse_payload
from uparse.assist.providers import build as build_provider
from uparse.assist.validate import Verdict, check
from uparse.config.schema import Config
from uparse.core.models import PageModel
from uparse.extraction.engine import ExtractionResult, extract
from uparse.logging import get_logger

log = get_logger("assist")

CACHE_DIR = Path.home() / ".cache" / "uparse" / "assist"
COMMENT_CHARS = 110


@dataclass(slots=True)
class Generated:
    brief: PageBrief
    verdict: Verdict
    jsonc: str
    provider: str
    fields: list[str] = field(default_factory=list)
    cached: bool = False
    error: str | None = None

    @property
    def assisted(self) -> bool:
        return self.error is None and self.verdict.ok

    @property
    def baseline(self) -> list[str]:
        """What the engine named on its own — the number the assisted run is measured against."""
        return [c.named for c in self.brief.columns if c.named]


def generate(
    page: PageModel, config: Config, *, want: str | None = None, cache: bool | None = None
) -> Generated:
    result = extract(page, config.extraction)
    brief = build_brief(page, result)

    verdict = Verdict(accepted=[], rejected=[])
    provider_name = "null"
    cached = False
    error: str | None = None

    if not brief.has_material:
        error = "the engine found no repeating structure on this page"
    else:
        try:
            provider = build_provider(config.assist)
            provider_name = getattr(provider, "name", "unknown")
            proposal, cached = _propose(
                provider,
                brief,
                want,
                use_cache=config.assist.cache if cache is None else cache,
            )
            if not proposal.fields:
                raise AssistError("the model's answer named no column the report describes")
            verdict = check(
                proposal,
                brief,
                result.records,
                min_coverage=config.assist.min_coverage,
                max_fields=config.assist.max_fields,
            )
        except AssistError as exc:
            error = str(exc)
            log.debug("assist unavailable: %s", exc)

    return Generated(
        brief=brief,
        verdict=verdict,
        jsonc=render(page, config, brief, verdict, result, want=want, error=error),
        provider=provider_name,
        fields=[name for name, _, _ in _entries(verdict, result)],
        cached=cached,
        error=error,
    )


def _propose(
    provider: Provider, brief: PageBrief, want: str | None, *, use_cache: bool
) -> tuple[Proposal, bool]:
    key = _cache_key(provider, brief, want)
    if use_cache:
        hit = _cache_read(key)
        if hit is not None:
            log.debug("assist cache hit %s", key[:12])
            return Proposal.from_payload(hit), True

    payload = _ask(provider, prompts.system(), prompts.user(brief, want))
    if use_cache:
        _cache_write(key, payload)
    return Proposal.from_payload(payload), False


def _ask(provider: Provider, system: str, user: str) -> dict[str, Any]:
    """One call, and — below tier SCHEMA — one repair.

    A provider that can be handed a JSON schema either honours it or has a real problem;
    retrying it wastes quota. A local model behind `ollama run` wraps its JSON in prose
    often enough that the single retry is what makes tier TEXT usable at all.
    """
    answer = provider.complete(system, user, prompts.SCHEMA)
    try:
        return parse_payload(answer)
    except AssistError:
        if getattr(provider, "tier", Tier.TEXT) is Tier.SCHEMA:
            raise
        log.debug("unreadable answer from %s; asking once for the object alone", provider.name)
    return parse_payload(provider.complete(system, prompts.repair(answer), prompts.SCHEMA))


# ------------------------------------------------------------------ output


def render(
    page: PageModel,
    config: Config,
    brief: PageBrief,
    verdict: Verdict,
    result: ExtractionResult,
    *,
    want: str | None = None,
    error: str | None = None,
) -> str:
    """Emit JSONC. Comments are the point: the file has to explain why it says what it says."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    lines: list[str] = [
        f"// Generated by `uparse generate` on {stamp}.",
        f"// Source: {_comment(page.base_url)}",
        f"// Engine: strategy {_comment(result.strategy)}, {len(result.records)} records.",
    ]
    if want:
        lines.append(f"// Asked for: {_comment(want)}")
    if error:
        lines.append(
            f"// Assist unavailable ({_comment(error)}) — fields below are the engine's own."
        )
    for item in verdict.rejected:
        lines.append(f"// Dropped {_comment(item.name, 40)!r}: {_comment(item.reason)}.")
    for skipped in verdict.ignored:
        lines.append(
            f"// Ignored {skipped.column}: interface text, not data"
            + (f" — e.g. {_comment(skipped.sample, 40)!r}." if skipped.sample else ".")
        )
    if verdict.notes:
        lines.append(f"// Model note: {_comment(verdict.notes)}")
    lines += ["// Review before running.", "{"]

    lines.append('  "sources": {')
    lines.append('    "type": "url",')
    lines.append(f'    "url": {json.dumps(page.base_url)}')
    lines.append("  },")
    lines.append("")

    if config.acquirer != "auto":
        lines.append(f'  "acquirer": {json.dumps(config.acquirer)},')
    lines.append('  "extraction": {')
    if verdict.collection:
        lines.append(f'    "collection": {json.dumps(verdict.collection)},')
    lines.append('    "fields": {')
    lines += _fields(verdict, result)
    lines.append("    }")
    lines.append("  },")
    lines.append("")
    lines.append('  "output": {')
    lines.append('    "format": "csv",')
    lines.append('    "path": "./records.csv"')
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _fields(verdict: Verdict, result: ExtractionResult) -> list[str]:
    entries = _entries(verdict, result)
    out: list[str] = []
    for index, (name, spec, note) in enumerate(entries):
        comma = "," if index < len(entries) - 1 else ""
        out.append(f"      {json.dumps(name)}: {spec}{comma}  // {note}")
    return out


def _entries(verdict: Verdict, result: ExtractionResult) -> list[tuple[str, str, str]]:
    """Name, spec and comment for every field the file will carry."""
    entries: list[tuple[str, str, str]] = []
    for item in verdict.accepted:
        # Pin a selector only where it adds something. Every refinement the engine applies
        # on the way out — ellipsis recovery, absolutization, type coercion — lives in the
        # value it produced, not in the selector, so re-reading the DOM throws them away.
        if item.agrees_with_engine or item.selector.startswith("(field:"):
            spec, note = '"auto"', item.why or "the engine already finds this"
        elif item.engine_name:
            spec = f'{{ "field": {json.dumps(item.engine_name)} }}'
            note = item.why or f"the engine's {item.engine_name}, under this name"
        else:
            spec = f'{{ "selector": {json.dumps(item.selector)} }}'
            note = item.why or f"{item.coverage:.0%} of records, e.g. {item.sample[:40]!r}"
        entries.append((item.name, spec, _comment(note)))

    if not entries:  # no model, or nothing survived: fall back to what the engine named
        for name in result.schema:
            entries.append((name, '"auto"', "inferred by the engine"))
    return entries


def _comment(text: str, limit: int = COMMENT_CHARS) -> str:
    """Flatten anything destined for a `//` comment.

    Field names, reasons and samples come from a model reading an untrusted page. A newline
    in one of them would end the comment and start a line of JSON, which is exactly the
    "the model emits something that is executed rather than validated" failure the design
    rules out. Collapse whitespace, drop controls, clip.
    """
    flat = " ".join(str(text).split())
    flat = "".join(c for c in flat if c.isprintable())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ------------------------------------------------------------------- cache


def _cache_key(provider: Provider, brief: PageBrief, want: str | None) -> str:
    blob = "\x00".join([getattr(provider, "name", "?"), brief.to_json(), want or ""])
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _cache_read(key: str) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{key}.json"
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None


def _cache_write(key: str, payload: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:  # a read-only home must not fail a run
        log.debug("cannot cache proposal: %s", exc)
