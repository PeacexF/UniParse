from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from uparse import __version__
from uparse.config.loader import build_config, is_config_path, load_config
from uparse.config.schema import Config
from uparse.core.errors import UparseError
from uparse.core.job import Job, build_acquirer
from uparse.core.models import JobStats, PageModel, PaginationHint
from uparse.exporters.writers import get_exporter
from uparse.extraction.engine import ExtractionResult, extract
from uparse.logging import console, die, setup_logging
from uparse.navigation.pagination import find_next
from uparse.sources.loader import resolve
from uparse.storage.sqlite import Store

CONTEXT = {"help_option_names": ["-h", "--help"], "max_content_width": 100}
GROUP_FLAGS = frozenset({"-h", "--help", "--version"})

Command = Callable[..., Any]


class TargetGroup(click.Group):
    # `uparse URL` means `uparse scrape URL`; anything unrecognized goes to scrape.
    default = "scrape"

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] not in GROUP_FLAGS:
            args = [self.default, *args]
        return super().parse_args(ctx, args)


def _stack(*options: Callable[[Command], Command]) -> Callable[[Command], Command]:
    def decorate(command: Command) -> Command:
        for option in reversed(options):
            command = option(command)
        return command

    return decorate


common_options = _stack(
    click.option("-v", "--verbose", count=True, help="More logging; repeat for more."),
    click.option("--debug", is_flag=True, help="Expose extraction reasoning."),
)

scrape_options = _stack(
    click.option(
        "-o",
        "--output",
        type=click.Path(path_type=Path),
        help="Output file; format from the extension.",
    ),
    click.option(
        "-f",
        "--format",
        "fmt",
        type=click.Choice(["json", "jsonl", "csv", "sqlite"]),
        help="Force the output format.",
    ),
    click.option("--fields", help="Comma-separated fields to keep, e.g. title,price,url."),
    click.option(
        "--db", type=click.Path(path_type=Path), help="Job database (defaults to in-memory)."
    ),
    click.option(
        "-c",
        "--config",
        "config_path",
        type=click.Path(exists=True, path_type=Path),
        help="JSONC job configuration.",
    ),
    click.option("--max-pages", type=int, help="Pagination limit per source."),
    click.option("-j", "--concurrency", type=int, help="Sources to process at once."),
    click.option("--per-host", type=int, help="Concurrent requests allowed per host."),
    click.option(
        "--follow",
        is_flag=True,
        help="Visit the page each record links to and merge its fields in.",
    ),
    click.option("--follow-field", help="Record field holding the link to follow (default: url)."),
    click.option("--no-robots", is_flag=True, help="Do not fetch or honour robots.txt."),
    click.option(
        "--no-browser", is_flag=True, help="Use plain HTTP instead of Chromium (no JavaScript)."
    ),
    click.option("--no-pagination", is_flag=True, help="Do not follow next-page links."),
    click.option(
        "--provenance", is_flag=True, help="Include confidence and source for every value."
    ),
    click.option("--delay", type=float, help="Seconds to wait between requests."),
)


@click.group(cls=TargetGroup, context_settings=CONTEXT)
@click.version_option(__version__, prog_name="uparse")
def main() -> None:
    """Extract structured data from arbitrary websites.

    \b
    uparse https://example.com/products
    uparse urls.txt --fields title,price --output products.csv
    uparse job.jsonc
    uparse inspect https://example.com/products
    """


@main.command(context_settings=CONTEXT)
@click.argument("target", required=False)
@scrape_options
@common_options
def scrape(target: str | None, **options: Any) -> None:
    """Extract records from a URL, a file of URLs, stdin, or a JSONC job."""
    setup_logging(options["verbose"], debug=options["debug"])
    if not target and not options.get("config_path"):
        die("nothing to scrape: pass a URL, a file of URLs, '-' for stdin, or -c job.jsonc")
        return
    if target and is_config_path(target):
        # `uparse job.jsonc` means the job file *is* the configuration.
        options, target = {**options, "config_path": Path(target)}, None
    try:
        config = _build_config(options)
        urls = _urls(config, target)
    except UparseError as exc:
        die(str(exc))
        return
    db_path = config.db or (config.output.path if config.output.format == "sqlite" else None)
    _run(config, urls, Store(db_path), config.output.path)


@main.command(context_settings=CONTEXT)
@click.argument("target")
@click.option("--schema", is_flag=True, help="Print the inferred schema as JSON.")
@click.option("--explain", metavar="FIELD", help="Show the signals behind one field.")
@click.option("--no-browser", is_flag=True, help="Use plain HTTP instead of Chromium.")
@click.option("-c", "--config", "config_path", type=click.Path(exists=True, path_type=Path))
@common_options
def inspect(
    target: str,
    schema: bool,
    explain: str | None,
    no_browser: bool,
    config_path: Path | None,
    **options: Any,
) -> None:
    """Analyze one page without producing a dataset."""
    setup_logging(options["verbose"], debug=options["debug"])
    config = _build_config({"no_browser": no_browser, "config_path": config_path})
    try:
        urls = _urls(config, target)
        acquirer = build_acquirer(config, urls)
        try:
            page = acquirer.fetch(urls[0])
        finally:
            acquirer.close()
    except UparseError as exc:
        die(str(exc))
        return

    result = extract(page, config.extraction)
    if schema:
        payload = {n: {"type": str(t), "confidence": c} for n, (t, c) in result.schema.items()}
        click.echo(json.dumps(payload, indent=2))
        return
    if explain:
        _explain(result, explain)
        return
    _report(page, result, config)


@main.command(context_settings=CONTEXT)
@click.argument("db", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", type=click.Path(path_type=Path))
@click.option("-c", "--config", "config_path", type=click.Path(exists=True, path_type=Path))
@click.option("--no-browser", is_flag=True)
@common_options
def retry(
    db: Path, output: Path | None, config_path: Path | None, no_browser: bool, **options: Any
) -> None:
    """Re-run the sources that failed in a previous job."""
    setup_logging(options["verbose"], debug=options["debug"])
    store = Store(db)
    if store.latest_job_id() is None:
        die(f"{db} contains no jobs")
        return
    job_id = store.latest_job_with_failures()
    failed = [str(row["url"]) for row in store.failed_sources(job_id)] if job_id else []
    if job_id is None or not failed:
        console.print("[ok]nothing to retry[/ok]")
        store.close()
        return
    store.mark_retried(job_id)
    console.print(f"retrying [score]{len(failed)}[/score] failed sources from job {job_id}")
    # The original job's configuration is stored with it; CLI flags only refine it.
    stored = build_config(store.job_config(job_id), origin=str(db))
    overrides = {"output": output, "config_path": config_path, "no_browser": no_browser}
    config = _apply_overrides(load_config(config_path) if config_path else stored, overrides)
    _run(config, failed, store, output or config.output.path)


# --------------------------------------------------------------------- run


def _run(config: Config, urls: list[str], store: Store, output: Path | None) -> None:
    job = Job(config, urls, store)
    try:
        stats = job.run()
    except UparseError as exc:
        die(str(exc))
        return
    finally:
        job.close()

    if config.output.format and config.output.format != "sqlite":
        exporter = get_exporter(config.output.format, config.output)
        rows = store.iter_provenance() if config.output.provenance else store.iter_records()
        written = exporter.write(rows, output)
        if output:
            console.print(f"wrote [score]{written}[/score] records to [url]{output}[/url]")
    elif output is None and store.path is None:
        _dump_stdout(store.iter_records())

    _summary(stats, store.path)
    store.close()
    if stats.pages_failed and not stats.records:
        sys.exit(1)


def _dump_stdout(rows: Iterator[dict[str, Any]]) -> None:
    get_exporter("jsonl").write(rows, None)


# ------------------------------------------------------------------ output


def _summary(stats: JobStats, db_path: Path | None) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="muted")
    table.add_column()
    table.add_row("Sources", str(stats.sources))
    table.add_row("Pages", f"{stats.pages_processed} processed, {stats.pages_failed} failed")
    if stats.pages_blocked:
        table.add_row("Blocked", f"[warn]{stats.pages_blocked}[/warn]")
    duplicates = f" ({stats.duplicates} duplicates dropped)" if stats.duplicates else ""
    table.add_row("Records", f"{stats.records}{duplicates}")
    if stats.followed:
        table.add_row("Followed", f"{stats.followed} detail pages")
    if stats.retries:
        table.add_row("Retries", str(stats.retries))
    table.add_row("Duration", _duration(stats.duration_s))
    if db_path:
        table.add_row("Database", str(db_path))
    console.print()
    console.print(table)
    if stats.errors_by_code:
        console.print()
        for code, n in sorted(stats.errors_by_code.items(), key=lambda kv: -kv[1]):
            console.print(f"  [fail]{code}[/fail] × {n}")


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def _report(page: PageModel, result: ExtractionResult, config: Config) -> None:
    console.print()
    console.print("[muted]PAGE[/muted]")
    console.print(f"  {page.final_url or page.url}")
    if page.title:
        console.print(f"  [muted]{page.title}[/muted]")

    console.print()
    console.print("[muted]STRUCTURES[/muted]")
    if result.collections:
        for cand in result.collections[:5]:
            marker = "→" if cand is result.collection else " "
            console.print(
                f"  {marker} {cand.label:<14} {cand.count:>4} items   "
                f"[score]{cand.confidence:.2f}[/score]   [muted]{cand.selector}[/muted]"
            )
    else:
        console.print("    [muted]none detected[/muted]")

    console.print()
    console.print("[muted]STRUCTURED DATA[/muted]")
    if result.structured_counts:
        for name, n in result.structured_counts.most_common(8):
            console.print(f"    {name:<28} {n:>4}")
    else:
        console.print("    [muted]none[/muted]")

    console.print()
    console.print(
        f"[muted]FIELDS[/muted]  [muted](strategy: {result.strategy}, "
        f"{len(result.records)} records)[/muted]"
    )
    for name, (vtype, confidence) in result.schema.items():
        sample = next((r.get(name) for r in result.records if r.get(name) is not None), "")
        console.print(
            f"    [field]{name:<14}[/field] {vtype!s:<9} [score]{confidence:.2f}[/score]"
            f"   [muted]{str(sample)[:44]}[/muted]"
        )
    if not result.schema:
        console.print("    [muted]no fields inferred[/muted]")

    console.print()
    console.print("[muted]PAGINATION[/muted]")
    hint = _pagination_hint(page, result, config)
    if hint:
        console.print(
            f"    {hint.method:<20} [score]{hint.confidence:.2f}[/score]   [url]{hint.url}[/url]"
        )
    else:
        console.print("    [muted]no next page detected[/muted]")
    console.print()
    console.print("[muted]run with --explain FIELD to see why a value was chosen[/muted]")


def _pagination_hint(
    page: PageModel, result: ExtractionResult, config: Config
) -> PaginationHint | None:
    if result.root is None:
        return None
    return find_next(result.root, page, config.pagination)


def _explain(result: ExtractionResult, field_name: str) -> None:
    for record in result.records:
        fv = record.fields.get(field_name)
        if fv is None:
            continue
        console.print()
        console.print(f"[field]{field_name}[/field] = {fv.value!r}")
        console.print(
            f"confidence: [score]{fv.confidence:.2f}[/score]   source: {fv.source}"
            f"   selector: [muted]{fv.selector}[/muted]"
        )
        console.print("signals:")
        for signal in fv.signals:
            detail = f"   [muted]{signal.detail}[/muted]" if signal.detail else ""
            console.print(f"  {signal.name:<30} {signal.weight:+.3f}{detail}")
        return
    die(f"no field named {field_name!r} was extracted; try `uparse inspect URL` first")


# ------------------------------------------------------------------ config


def _build_config(options: dict[str, Any]) -> Config:
    path = options.get("config_path")
    return _apply_overrides(load_config(path) if path else Config(), options)


def _apply_overrides(config: Config, options: dict[str, Any]) -> Config:
    overrides: dict[str, Any] = {}

    if options.get("output"):
        overrides.setdefault("output", {})["path"] = str(options["output"])
    if options.get("fmt"):
        overrides.setdefault("output", {})["format"] = options["fmt"]
    if options.get("provenance"):
        overrides.setdefault("output", {})["provenance"] = True
    if options.get("fields"):
        names = [f.strip() for f in str(options["fields"]).split(",") if f.strip()]
        overrides["extraction"] = {"fields": dict.fromkeys(names, "auto")}
    if options.get("max_pages"):
        overrides.setdefault("pagination", {})["max_pages"] = options["max_pages"]
    if options.get("no_pagination"):
        overrides.setdefault("pagination", {})["enabled"] = False
    if options.get("concurrency"):
        overrides["concurrency"] = options["concurrency"]
        overrides.setdefault("browser", {})["concurrency"] = options["concurrency"]
    if options.get("per_host"):
        overrides.setdefault("politeness", {})["per_host"] = options["per_host"]
    if options.get("follow") or options.get("follow_field"):
        follow: dict[str, Any] = {"enabled": True}
        if options.get("follow_field"):
            follow["field"] = options["follow_field"]
        overrides["follow"] = follow
    if options.get("no_robots"):
        overrides.setdefault("politeness", {})["respect_robots"] = False
    if options.get("no_browser"):
        overrides.setdefault("browser", {})["enabled"] = False
        overrides["acquirer"] = "http"
    if options.get("delay") is not None:
        overrides.setdefault("politeness", {})["delay_s"] = options["delay"]
    if options.get("db"):
        overrides["db"] = str(options["db"])
    return config.merged(**overrides) if overrides else config


def _urls(config: Config, target: str | None) -> list[str]:
    return resolve(config, target)


if __name__ == "__main__":
    main()
