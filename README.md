<div align="center">

<img src=".github/img/logo.png" width="300" alt="UniParse">

# UniParse

**Extract structured records from arbitrary websites with little or no per-site configuration**

[![Python](https://img.shields.io/badge/Python-3.14%2B-0C0D11?logo=python&logoColor=white)](https://www.python.org/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.9-0C0D11?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-0C0D11?logo=googlechrome&logoColor=white)](https://playwright.dev/)
[![SQLite](https://img.shields.io/badge/SQLite-WAL-0C0D11?logo=sqlite&logoColor=white)](https://www.sqlite.org/)

![Status](https://img.shields.io/badge/status-v1-3E73FE)
![License](https://img.shields.io/badge/license-AGPL--3.0-3E73FE)

</div>

---

```bash
uparse https://books.toscrape.com --output books.csv
```
```
[1/1] books.toscrape.com/ ✓ 500 records over 25 pages
wrote 500 records to books.csv

Sources   1
Pages     25 processed, 0 failed
Records   500
Duration  00:17
```

No selectors were written. The engine found the product grid, named the fields, followed
pagination to the end, normalized the prices and wrote a CSV.

When it gets something wrong — and on unusual sites it will — you pin that one field in a
config file and run it again. That loop is the whole product:

```text
try automatic  →  inspect the result  →  override what broke  →  deliver
```

---

## Contents

- [Install](#install)
- [The four things it does](#the-four-things-it-does)
- [What it extracts, and in what order](#what-it-extracts-and-in-what-order)
- [When it gets it wrong](#when-it-gets-it-wrong)
- [Listing → detail](#listing--detail)
- [Speed and politeness](#speed-and-politeness)
- [Output](#output)
- [Reliability](#reliability)
- [Development](#development)
- [Limitations worth knowing](#limitations-worth-knowing)

---

## Install

Needs **Python 3.14**, [uv](https://docs.astral.sh/uv/), and **Node 20+** for the browser worker.

```bash
git clone https://github.com/PeacexF/UniParse && cd UniParse
make setup          # python env + npm install + chromium
make check          # lint, types, tests
```

`make setup` downloads Chromium (~150 MB). To skip it and use the HTTP-only path, run
`make venv` instead and pass `--no-browser` to every command.

---

## The four things it does

```bash
# a single page or a whole paginated listing
uparse https://example.com/products --output products.csv

# a file of URLs, keeping only the fields you want
uparse urls.txt --fields title,price,image,url --output products.db

# a saved job, with every setting pinned
uparse job.jsonc

# and the debugging view, which is where you spend the awkward 20%
uparse inspect https://example.com/products --explain price
```

Input can be a URL, a file of URLs (`#` comments allowed), `-` for stdin, a local
`.html` file, or a `.jsonc` job. Output format comes from the extension — `.json`,
`.jsonl`, `.csv`, `.db` — or `--format`.

---

## What it extracts, and in what order

Every strategy states a confidence and the highest wins, so a layout table can no longer
pre-empt a well-formed product grid:

| | strategy | typical confidence |
|---|---|---|
| 1 | Explicit configuration | 1.00 |
| 2 | JSON-LD, microdata, OpenGraph, embedded app state | 0.95 |
| 3 | Data tables (header inference, colspan/rowspan aware) | 0.30–0.95 |
| 4 | Repeated DOM structures found by fingerprint clustering | 0.00–1.00 |

Within a collection, fields come from semantic tags, `itemprop`, a class-name vocabulary,
text patterns — and from **cross-record alignment**, which reads the collection as a table:
a relative selector that resolves in most records is a column, and a column whose values
all parse the same way is a typed field. That last mechanism is what keeps working on
sites built with CSS-in-JS or utility classes, where no class name means anything:

```
$ uparse inspect https://quotes.toscrape.com --no-browser

FIELDS  (strategy: dom, 10 records)
    author         string    0.81   Albert Einstein
    category       string    0.63   deep-thoughts
    description    string    0.52   “The world as we have created it is a proces
    url            url       0.50   https://quotes.toscrape.com/tag/deep-thought
    title          string    0.22   deep-thoughts
```

Sites also name their own fields, and namespaced class tokens are trusted to do it:
`span.country-capital` becomes `capital` without anything in the vocabulary knowing
what a capital is.

---

## When it gets it wrong

`inspect` tells you what it found and why:

```bash
uparse inspect https://example.com/products              # collections, fields, pagination
uparse inspect https://example.com/products --explain price
```
```
price = 51.77
confidence: 0.78   source: classname
selector:   article.product_pod > div.product_price > p.price_color
signals:
  source:classname               +0.320   'price'
  field name match               +0.150   price
  value parses as a number       +0.150   £51.77
  collection consistency         +0.160   100% of items
```

The selector it prints is **record-relative**, so it goes straight into a config:

```jsonc
{
  "extraction": {
    "fields": {
      "title": { "selector": "h3 a" },              // text of that anchor
      "url":   { "selector": "h3 a" },              // its href — url fields take the link
      "sku":   { "selector": ".code", "attribute": "data-sku" },
      "price": "auto"                               // leave the rest inferred
    }
  }
}
```

A pinned selector is resolved **inside each record**, not once per page. `attribute` reads
a real attribute, or the pseudo-attributes `text` and `html` when you want the text of
something the engine would otherwise treat as a link.

Explicit configuration always wins. See [docs/configuration.md](docs/configuration.md) for
every option and [examples/](examples/) for three complete jobs.

---

## Listing → detail

Most real jobs need the page behind each row. `--follow` visits it and merges what it
finds back into the record:

```bash
uparse https://books.toscrape.com --follow --output books.jsonl
```
```json
{"url": "…/a-light-in-the-attic_1000/index.html", "title": "A Light in the Attic",
 "price": 51.77, "availability": "In stock (22 available)", "sku": "a897fe39b1053632",
 "product_type": "Books", "price_excl_tax": "£51.77", "tax": "£0.00",
 "description": "It's hard to imagine a world without…"}
```

Five fields from the listing became eleven. Detail pages are treated as one entity rather
than a collection, and a two-column `SKU | a897fe…` table is read as that product's
properties rather than as rows.

---

## Speed and politeness

```bash
uparse urls.txt -j 8 --output out.csv     # 8 sources at once
```

Sources run in parallel; pagination within a source stays sequential because the next page
is only discoverable from the current one. Output order follows the input, so a job
produces the same file at any worker count.

Politeness is enforced **per host**, not per job: `politeness.per_host` (default 2) bounds
in-flight requests to any one site and `--delay` spaces them, so eight threads across eight
sites never becomes a burst against one of them. `robots.txt` is fetched once per host and
honoured, including `Crawl-delay`; `--no-robots` opts out.

On a challenge page the run reports `BLOCKED` and moves on. UniParse detects blocks and
never tries to defeat them — no CAPTCHA solving, no fingerprint spoofing. That is a
permanent boundary, not a missing feature.

---

## Output

`.db` is the job itself: records, fields, pages, errors and full provenance, ready to hand
to a client or query directly.

```sql
SELECT json_extract(data_json, '$.title'), json_extract(data_json, '$.price')
FROM records ORDER BY id;
```

`--provenance` adds confidence, source and selector to every value in JSON/JSONL output.
JSON, JSONL and SQLite stream; CSV buffers, because it needs the full header first.

---

## Reliability

One bad page never kills a job. Failures are categorized (`NAVIGATION_TIMEOUT`,
`HTTP_ERROR`, `BLOCKED`, `ROBOTS_DISALLOWED`, …) and stored, transient ones are retried
with exponential backoff, and Ctrl-C flushes and prints the summary. Afterwards:

```bash
uparse retry products.db     # re-runs only what failed, reusing the stored job config
```

---

## Development

```bash
make check        # lint + types + tests
make test-all     # everything, including browser integration tests
make eval         # score the engine against the saved corpus
make eval ARGS=-v # ... and show every mismatch
```

`tests/corpus/` holds real pages with hand-written expectations, and `tools/eval.py` turns
"did that change help?" into a number. Scoring weights move on evidence from that corpus,
not on whichever site was looked at last.

[docs/architecture.md](docs/architecture.md) explains how the pieces fit together.

---

## Limitations worth knowing

- **Number parsing is heuristic.** A single separator followed by exactly three digits is
  read as grouping, so `1.299` → `1299`. `12.500` meaning twelve-and-a-half is misread.
  Set an explicit `type` to opt out.
- **Scoring weights are internal.** Not a public API; expect them to move.
- **No JS execution without the worker.** `--no-browser` is fast and fine for static
  pages, and useless on anything that renders client-side.
- **Sites change.** The corpus is a regression net for the engine, not a promise about
  any particular site.

## License · Contributing · Security

[LICENSE](LICENSE) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)
