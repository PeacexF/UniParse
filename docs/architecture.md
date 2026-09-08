# Architecture

How the pieces fit, and why they are shaped the way they are. For what the knobs do, see
[configuration.md](configuration.md).

## The pipeline

```text
sources ──► acquirer ──► PageModel ──► extract ──► validate ──► dedupe ──► SQLite ──► export
               ▲                          │                                  ▲
               │                          ▼                                  │
               └──────── pagination ◄── next-page hint          follow ──────┘
                                                              (detail pages)
```

| Layer | Module | Responsibility |
|---|---|---|
| Sources | `sources/loader.py` | URL, URL file, stdin, local HTML, JSONC job |
| Acquisition | `acquisition/{base,http,browser}.py` | Fetch a page, detect blocks, return a `PageModel` |
| Robots | `acquisition/robots.py` | One robots.txt per host, honoured |
| Extraction | `extraction/*` | Structured data, tables, DOM collections, fields, alignment |
| Scoring | `processing/scoring.py` | Additive, deterministic, explainable |
| Navigation | `navigation/pagination.py` | rel=next, link text, numbered pagers, URL patterns |
| Processing | `processing/{normalize,deduplicate,validation}.py` | Clean, coerce, dedupe |
| Limits | `core/limits.py` | Per-host concurrency and pacing |
| Storage | `storage/sqlite.py` | WAL, streaming inserts, provenance |
| Export | `exporters/writers.py` | JSON, JSONL, CSV (SQLite is the job DB) |
| Runner | `core/{pipeline,job}.py` | Threads, retries, error isolation, follow, Ctrl-C |
| CLI | `cli.py` | `scrape` (default), `inspect`, `retry` |

## Five decisions that shape everything

### Extraction never learns how a page was acquired

Everything is a `PageModel`: url, html, status, title, and some metadata. Chromium, plain
HTTP and a file on disk are indistinguishable downstream. That is what lets the whole test
suite run against saved HTML with no network, and why adding an acquirer costs nothing
elsewhere.

### Strategies compete, they do not queue

Each strategy states a quality and the best one wins (`engine._arbitrate`):

| strategy | quality |
|---|---|
| configured collection | 1.00 |
| structured data (JSON-LD, microdata) | 0.95 |
| data table | 0.30–0.95, scored on header, column evenness, nesting |
| DOM collection | the collection's own confidence |

This replaced a first-non-empty chain, under which a layout table beat a 0.94-confidence
product grid simply because tables were tried earlier. `result.notes` records the choice,
so `inspect` can show what lost.

### Fields come from evidence, not only from vocabulary

Three sources, in rough order of trust:

1. **Named** — `itemprop`, semantic tags, and a curated class-name vocabulary
   (`extraction/vocabulary.py`). Precise where sites use conventional names.
2. **Patterned** — prices, dates, ratings, SKUs (`extraction/patterns.py`). Only from
   elements that *state* the value; an amount inside a sentence is a mention.
3. **Aligned** — `extraction/alignment.py` reads a collection as a table. A relative
   selector resolving in ≥60% of records is a column; a column whose values all parse as
   dates is a `date`; the longest varying column is a `description`; a namespaced class
   token names its own column (`span.country-capital` → `capital`).

Alignment is what survives CSS-in-JS, where (1) and (2) find nothing. It deliberately
refuses to name titles from value shape alone — that produced confident nonsense.

Every candidate is scored additively and the best per field wins. Two guards matter:
`_implausible` drops a value that cannot be what its name claims (a `mobile_comments`
class is not a phone number), and `_demote_containers` penalizes a candidate that wraps a
more specific one, so `div.byline` loses to the `<a>` inside it.

### Selectors are record-relative

Provenance selectors are computed against the record node, not the document. Three
consequences: `inspect` prints something you can paste into a config; a configured
selector resolves inside each record instead of matching the first hit on the page; and
consistency scoring works, because the same field has the same selector in every record.

### One bad page never kills a job

Every failure is categorized (`core/errors.py`) and stored. Transient codes are retried
with exponential backoff and jitter; terminal ones (`BLOCKED`, `ROBOTS_DISALLOWED`) are
recorded and skipped. Extraction runs inside a `try` so a malformed page costs one page.

## Concurrency

Sources run in a `ThreadPoolExecutor` bounded by `concurrency`. Pagination inside a source
stays sequential — the next page is only discoverable from the current one.

Three pieces of shared state are guarded: `Store` (one connection, every statement under a
lock, transactions held across BEGIN/COMMIT), `Deduplicator` (the seen-set decides whether
a record is written at all), and `JobStats`. Export orders by `sources.id, pages.id,
record_index` rather than insertion, so **the same job produces the same file at any worker
count** — there is a test asserting `-j 1` and `-j 8` are byte-identical.

Politeness is per host, not per job (`core/limits.py`): a semaphore bounds in-flight
requests to one site and a monotonic schedule spaces them. Threads claim their host's next
slot under a lock and sleep outside it, so waiting on one host never blocks another.

`follow` uses a **separate** pool. Source threads wait on detail fetches and detail fetches
never queue more work, so the two pools cannot deadlock each other.

## The browser worker

Python spawns `node browser/dist/main.js` and speaks newline-delimited JSON over
stdin/stdout. **stdout carries protocol frames only**; diagnostics go to stderr.

```text
Python                                  Node
  ├─ hello      {protocol, headless, …}  ─►  launch Chromium, build the page pool
  ├─ navigate   {url, waitUntil, …}      ─►  goto, wait, optional scroll, return html
  │                                      ◄─  {url, status, title, html, scrolls, pageId}
  └─ shutdown                            ─►  close pages, contexts, browser
```

Requests are correlated by id, not serialized: a reader thread pumps stdout and dispatches
to per-request slots, so several navigations are in flight at once. `pageId` comes back on
every navigate, which is what makes a follow-up `evaluate` / `click` / `scroll` on that
same page possible.

The worker owns one Chromium for the job, pools pages up to `browser.concurrency`, and
discards a page that failed rather than returning it to the pool — a timed-out `goto`
leaves navigation in flight and poisons the next one.

Two rules keep a worker from outliving its parent, learned the hard way:

- **Exit on a broken pipe.** When the parent dies both pipes break; writing to a broken
  pipe raises `EPIPE`, and because diagnostics go to stderr the error handler's own log
  raises `EPIPE` again. That loop pins a core. Once output has nowhere to go there is
  nothing to report to, so the worker exits.
- **Watch the parent.** Being reparented to init is unambiguous. Closing Chromium is worth
  a try but never worth waiting on, so the shutdown is time-boxed and then hard-exits.

On the Python side, the worker is created under a lock. Without it, every thread that
raced past the `is None` check spawned its own node + Chromium, and only the last
assignment survived; the rest were unreachable and never closed.

## Storage

Six tables — `jobs`, `sources`, `pages`, `records`, `fields`, `errors` — plus `meta` for
the schema version. Records store their JSON payload; `fields` keeps one row per value
with confidence, source, selector and full signal provenance. That is what `--provenance`
exports and what makes a delivered `.db` auditable.

Everything goes through `Store`, never raw SQL in the pipeline, so the PostgreSQL move
described in the roadmap stays a swap of one module.

## Testing

- **Unit** — extraction, scoring, normalization, pagination, limits. No network.
- **Integration** — a real HTTP server over a generated site: pagination, detail pages,
  a robots.txt with a disallowed path, a challenge page, JS-rendered content.
- **Browser** — behind `@pytest.mark.browser`, skipped rather than failed without a build.
- **Corpus** — `tests/corpus/` holds real pages, gzipped, with hand-written expectations.
  `tools/eval.py` scores them (`make eval`); `tests/test_corpus.py` makes CI fail on a
  regression.

The corpus is the project's actual moat. Scoring weights move on evidence from it, not on
whichever site was looked at last.
