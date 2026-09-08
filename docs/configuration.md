# Configuration

A job is a JSONC file — JSON with `//` comments and trailing commas — passed as the target:

```bash
uparse job.jsonc
uparse urls.txt -c settings.jsonc --output out.csv   # config plus a CLI target
```

CLI flags are merged **over** the file, so a saved job can be nudged without editing it.
Every section is optional; the defaults below are what you get with none of it.

Complete jobs live in [examples/](../examples).

---

## sources

Where URLs come from. A CLI target overrides this entirely.

```jsonc
"sources": { "type": "url",  "url": "https://example.com/products" }
"sources": { "type": "urls", "urls": ["https://a.test/1", "https://a.test/2"] }
"sources": { "type": "file", "path": "./urls.txt" }   // '#' comments and blanks ignored
"sources": { "type": "stdin" }
```

| key | default | |
|---|---|---|
| `type` | `"url"` | `url` · `urls` · `file` · `stdin` |
| `url` | – | single URL for `type: "url"` |
| `urls` | `[]` | list for `type: "urls"` |
| `path` | – | required for `type: "file"` |

## browser

Chromium, via the Node worker. Ignored when `acquirer` resolves to `http` or `file`.

| key | default | |
|---|---|---|
| `enabled` | `true` | `false` is the same as `--no-browser` |
| `headless` | `true` | `false` opens a visible window — useful when a page misbehaves |
| `timeout` | `30000` | ms, per navigation |
| `concurrency` | `4` | pages pooled inside the worker |
| `wait_until` | `"domcontentloaded"` | `load` · `domcontentloaded` · `networkidle` · `commit` |
| `wait_for` | – | CSS selector to wait for after load |
| `wait_ms` | `0` | flat extra wait, the blunt instrument |
| `user_agent` | – | overrides the default UA |
| `viewport` | `[1366, 900]` | |
| `locale` / `timezone` | – | e.g. `"de-DE"`, `"Europe/Berlin"` |
| `block_resources` | `["font", "media"]` | any of font, media, image, stylesheet, script, xhr, fetch, websocket, other |
| `block_images` | `false` | images are often the target, so this is opt-in |
| `persist_cookies` | `true` | one shared context for the job |
| `storage_state` | – | path to a Playwright storage-state file, loaded and saved |
| `executable_path` | – | your own Chromium |
| `extra_headers` | `{}` | added to every request |

`document` can never be blocked — aborting it aborts the navigation.

## extraction

```jsonc
"extraction": {
  "collection": ".product-card",     // pin the record container
  "fields": {
    "title": { "selector": "h3 a" },
    "price": "auto"
  }
}
```

| key | default | |
|---|---|---|
| `mode` | `"auto"` | |
| `collection` | – | CSS selector for the record container; skips discovery |
| `fields` | `{}` | see below. Naming any field **restricts output to those fields** |
| `min_confidence` | `0.0` | drop records scoring below this |
| `min_records` | `2` | fewer repeats than this is not a collection |
| `max_records_per_page` | `5000` | |
| `include_structured_data` | `true` | JSON-LD, microdata, OpenGraph, embedded state |
| `include_tables` | `true` | |
| `provenance` | `false` | keep signals in memory (`output.provenance` exports them) |

### Field specs

Four spellings, increasingly explicit:

```jsonc
"fields": {
  "price": "auto",                                    // infer it
  "title": ".product-title",                          // shorthand for a selector
  "sku":   { "selector": ".code", "attribute": "data-sku" },
  "stock": { "selector": ".qty", "type": "integer", "required": true }
}
```

| key | | |
|---|---|---|
| `mode` | `auto` · `selector` · `attribute` · `regex` · `constant` | inferred from the other keys |
| `selector` | CSS, resolved **inside each record** | |
| `attribute` | an attribute name, or the pseudo-attributes `text` / `html` | |
| `regex` | first group, or whole match; searched in the record's text | |
| `value` | a constant | |
| `type` | `auto` · `string` · `number` · `integer` · `boolean` · `url` · `date` · `datetime` | |
| `required` | `false` | records missing it are dropped |
| `many` | `false` | collect every match instead of the first |
| `default` | – | used when the selector finds nothing |

**Text or link?** A bare selector yields the element's text, unless the field's type is a
URL (`url`, `image`), in which case it yields `href`/`src`. Override either way with
`attribute`:

```jsonc
"title": { "selector": "h3 a" },                       // "A Light in the Attic"
"url":   { "selector": "h3 a" },                       // "https://…/a-light-in-the-attic/"
"label": { "selector": "h3 a", "attribute": "text" },  // text, even for a url-ish field
"link":  { "selector": "h3 a", "attribute": "href" }   // explicit
```

`href`, `src`, `srcset` and `data-src` come back absolute.

## pagination

| key | default | |
|---|---|---|
| `enabled` | `true` | |
| `selector` | – | CSS for the next link; beats all detection |
| `url_template` | – | e.g. `"https://x.test/list?page={page}"` |
| `max_pages` | `25` | per source |
| `infinite_scroll` | `false` | browser only |
| `max_scrolls` | `20` | |
| `max_items` | `10000` | stop once a source has produced this many |
| `max_duration_s` | `300` | per source |
| `stop_on_duplicate_page` | `true` | stop when a page repeats content already seen |

Detection order: configured selector → `rel="next"` → link text (multilingual, including
`›` `»` `→`) → numbered pagers → URL patterns (`?page=`, `/page/N`, `?p=`, `offset=`).
Visited URLs and content hashes both guard against loops.

## follow

Visit the page each record links to and merge what it says back into the record.

```jsonc
"follow": { "enabled": true, "field": "url", "max_pages": 500 }
```

| key | default | |
|---|---|---|
| `enabled` | `false` | |
| `field` | `"url"` | which record field holds the link |
| `max_pages` | `200` | job-wide budget |
| `prefer` | `"detail"` | who wins a conflict, `detail` or `listing` |
| `same_host` | `true` | never wander off-site |

Detail pages are extracted as a single entity, not a collection, and two-column
label/value tables are read as that entity's properties. The listing's `url` is always
kept, and a detail `<title>` that is just the listing title plus a site name is ignored.

## dedupe

| key | default | |
|---|---|---|
| `enabled` | `true` | |
| `keys` | `[]` | fields forming the identity; overrides the defaults |
| `scope` | `"job"` | `job` · `source` · `page` |

Without `keys`, identity is the canonical URL, then `sku`, then `email`, then a hash of
the normalized record. Never the title alone.

## politeness

| key | default | |
|---|---|---|
| `delay_s` | `0.0` | minimum seconds between requests **to the same host** |
| `respect_robots` | `true` | fetch and honour robots.txt |
| `obey_crawl_delay` | `true` | use robots.txt `Crawl-delay` when longer than `delay_s` |
| `per_host` | `2` | concurrent requests allowed to one host |
| `user_agent_note` | – | free text, stored with the job |

A disallowed URL is recorded as `ROBOTS_DISALLOWED` and skipped. An unreachable robots.txt
is not a prohibition.

## retry

| key | default | |
|---|---|---|
| `attempts` | `3` | |
| `base_delay_s` | `1.0` | doubles each attempt |
| `max_delay_s` | `30.0` | |
| `jitter` | `0.25` | ± fraction |

Only transient codes are retried. `BLOCKED`, `ROBOTS_DISALLOWED` and config errors are not.

## output

| key | default | |
|---|---|---|
| `format` | from the extension | `json` · `jsonl` · `csv` · `sqlite` |
| `path` | – | stdout as JSONL when omitted |
| `provenance` | `false` | confidence, source and selector for every value |
| `pretty` | `true` | JSON indentation |
| `csv_delimiter` | `","` | |
| `columns` | `[]` | fix the CSV column order |

## top level

| key | default | |
|---|---|---|
| `concurrency` | `4` | sources processed at once; per-host limits still apply |
| `acquirer` | `"auto"` | `auto` · `browser` · `http` · `file` |
| `db` | – | job database; in-memory when omitted |
| `name` | – | label stored with the job |

---

## CLI flags

Everything below overrides the file.

```text
-o, --output PATH        -f, --format FMT       --fields a,b,c
    --db PATH            -c, --config PATH      --max-pages N
-j, --concurrency N          --per-host N       --delay SECONDS
    --follow                 --follow-field F   --no-robots
    --no-browser             --no-pagination    --provenance
-v, --verbose            --debug
```

`uparse inspect URL` takes `--schema`, `--explain FIELD`, `--no-browser` and `-c`.
`uparse retry job.db` takes `-o`, `-c` and `--no-browser`.

---

## A worked override

Automatic extraction gets the grid but calls the subtitle a description and misses the SKU:

```bash
uparse inspect https://shop.test/products --explain description
```

Pin those two, leave the rest inferred:

```jsonc
{
  "sources": { "type": "url", "url": "https://shop.test/products" },
  "extraction": {
    "fields": {
      "title": "auto",
      "price": "auto",
      "description": { "selector": ".summary" },
      "sku": { "selector": "[data-sku]", "attribute": "data-sku" }
    }
  },
  "output": { "format": "csv", "path": "./products.csv" }
}
```

Naming fields restricts the output to exactly those four columns.
