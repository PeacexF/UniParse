# Fixtures and corpus

Two kinds of test data, with different jobs.

## `fixtures/html/` — small, hand-written, one idea each

Minimal pages that isolate a single behaviour. Read them to understand what a feature is
supposed to do; they are small enough to hold in your head.

| file | what it pins down |
|---|---|
| `product_listing.html` | a clean product grid: the happy path |
| `product_jsonld.html` | JSON-LD and microdata describing one product, merged into one record |
| `news_table.html` | a real data table with a header row |
| `messy_listing.html` | hashed CSS-module class names, German number and date formats, relative URLs |

Add one whenever a bug turns out to be about *markup shape* rather than about a specific
site. Keep them small and keep the file about one thing.

## `corpus/` — real pages, saved, with expectations

Whole pages as they were actually served, gzipped, each with a `.json` file saying what
correct extraction looks like. This is the regression net: it is what stops a scoring
tweak that helps one site from quietly ruining four others.

```bash
make eval             # scoreboard
make eval ARGS=-v     # every mismatch
```

`tests/test_corpus.py` runs the same checks so CI fails on a regression.

### What an expectation file says

```jsonc
{
  "url": "https://books.toscrape.com/",
  "note": "Bootstrap listing. Titles are clipped in the visible text and complete in title=.",
  "expect": {
    "strategy": "dom",              // which strategy must win
    "records": 20,                  // exact count (or "min_records")
    "required_fields": ["title", "url", "price", "image"],
    "coverage": 0.9,                // fraction of records each required field must appear in
    "absent_fields": ["column_1"],  // things that must NOT be extracted
    "first_record": {               // exact values, or {"contains": …} / {"startswith": …}
      "title": "A Light in the Attic",
      "price": 51.77
    }
  }
}
```

`absent_fields` matters as much as the positive checks. Several of these pages are here
because the engine used to extract confident nonsense from them — a layout table read as
data, a CSS utility class read as a field name, a currency amount inside a sentence read
as a price. Those checks are what keep that from coming back.

### The pages, and why each is here

| page | what it defends |
|---|---|
| `books_toscrape` | the happy path, plus titles clipped with the full string in `title=` |
| `quotes_toscrape` | the main content has no vocabulary hook; only alignment finds it |
| `webscraper_ecommerce` | microdata where `<a itemprop="name">` must yield text, not its href |
| `scrapethissite_countries` | 250 items wrapped 3-per-row by a Bootstrap grid; fields the page names itself |
| `hackernews_front` | a layout table that must not pre-empt the real DOM collection |
| `wikipedia_largest_companies` | row headers and colspan/rowspan, the shape that shifted every column |
| `blogger_python_insider` | Tailwind utility classes, none of which may become a field name |
| `realpython_home` | a card grid with dates and images |
| `lobsters_front` | dense byline markup that produced a phantom phone number |
| `bbc_news_front` | CSS-in-JS throughout: only structure can name anything |
| `github_trending` | utility classes plus a description that must survive naming |

### Adding a page

```python
import gzip, pathlib, httpx

html = httpx.get(URL, follow_redirects=True).text
pathlib.Path("tests/corpus/my_page.html.gz").write_bytes(gzip.compress(html.encode(), 9))
```

Then write `my_page.json` by hand, from what the page *actually* contains — open it and
check. Never generate expectations from current output: that enshrines today's bugs as
tomorrow's contract.
