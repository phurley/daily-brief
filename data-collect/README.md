# Daily Brief data collection

Scheduled crawler that turns `source.json` into a local corpus of source
content, with a SQLite log of every attempt.

## Setup

```sh
cd data-collect
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/crawl4ai-setup          # downloads the Chromium browser (~200 MB)
```

## Usage

```sh
.venv/bin/python crawl_sources.py             # crawl sources that are due
.venv/bin/python crawl_sources.py --force     # crawl every source now
.venv/bin/python crawl_sources.py --dry-run   # list what would be crawled
.venv/bin/python crawl_sources.py --status    # last run per source
.venv/bin/python crawl_sources.py --errors    # sources failing their latest run
.venv/bin/python crawl_sources.py --prune 50  # trim the log to 50 rows/source
```

Run from cron/launchd as often as the shortest frequency (e.g. hourly); the
crawler itself decides what is due.

## How it decides what to crawl

Each source has a `frequency` in `source.json` (`hourly`, `4hours`, `daily`,
`weekly`, `monthly`). A source is crawled when its **last successful** run is
older than that period. `--force` ignores the schedule. Because scheduling keys
off success, a source that fails but succeeded recently is not hammered every
run; once it is overdue it is retried until it succeeds.

## What it collects

The crawler is deliberately permissive ("too much beats too little"); relevance
filtering happens downstream. Collection follows a cheapest-first ladder:

1. **Feeds** (`feed_urls`) are fetched first over plain HTTP: RSS/Atom, ICS,
   JSON. They are cheap, structured, and dated.
2. **HTTP crawl**: the `url`, `events_url`, and `crawl_urls` are deep-crawled
   with crawl4ai's HTTP strategy. When that client is blocked or empty, the
   seeds are retried with **curl_cffi**, which impersonates Chrome's TLS/JA3
   fingerprint and clears many WAFs (Cloudflare bot management, DataDome).
3. **Browser crawl**: only if the HTTP result looks blocked (401/403/429),
   empty, JS-shell-like, or contains no date-like content *and* no feed
   provided content. This is the only tier that launches Chromium.
4. **Commercial scraping API** (optional, off by default): used only as a last
   resort when every tier above still produced no dated content.

All tiers produce the **same output**: every document (feed item, HTTP page, or
browser page) is rendered through crawl4ai's HTML→markdown pipeline, so
`content.md` and `meta.json` have one consistent shape regardless of input.
Non-HTML inputs (PDF, ICS, JSON) are converted to text into the same structure.

- **Two seeds per source**: the main `url` and, when present, `events_url`
  (plus any `crawl_urls`).
- **Relevance-scored deep crawl** up to `--max-depth` / `--max-pages`.
- **Raw markdown**, unfiltered (no content pruning).
- **Linked non-HTML resources** are fetched too: PDFs (monthly calendars,
  agendas), ICS calendars, RSS/Atom feeds, and JSON.

## Per-source collection config

Each entry in `source.json` may override how it is collected. All fields are
optional; the default is the feeds → HTTP → browser ladder above.

| Field | Meaning |
| --- | --- |
| `engine` | `auto` (default): feeds, then HTTP crawl, escalating to the browser only when needed. `http`: feeds + HTTP crawl, never a browser. `browser`: force the headless browser. All produce the same output. |
| `crawl_urls` | Extra seeds crawled in addition to `url`/`events_url` (e.g. a Drupal `events-feed/upcoming` route). |
| `feed_urls` | RSS/Atom/ICS/JSON URLs fetched over plain HTTP (preferred, dated, cheap). A public **Instagram profile** URL also works: recent post captions (often carrying event dates and ticket links) are extracted. |
| `max_pages` | Per-source override of the global `--max-pages` budget, to bound pathologically slow/paywalled sources. |

A source may also have feeds/PDFs auto-discovered from its HTML; those are
fetched automatically, bounded by `--max-assets`.

Example (AADL, whose `/events` page is a JS shell but which exposes
server-rendered event routes and a feed):

```json
{
  "name": "Ann Arbor District Library (AADL)",
  "url": "https://aadl.org",
  "events_url": "https://aadl.org/events",
  "engine": "http",
  "crawl_urls": [
    "https://aadl.org/events-feed/upcoming",
    "https://aadl.org/events-feed/exhibits"
  ],
  "feed_urls": ["https://aadl.org/rss.xml"]
}
```

## Feeds

Feeds are first-class. `feed_urls` are always fetched first (independent of
`--no-assets`), rendered by `_parse_feed`, and summarized into
`meta.crawl.feeds` for downstream joins.

**Feed markdown shape** (`content.md`). One item per `-` line, followed by
indented `key: value` continuations:

```
# Feed: https://example.com/feed/   (rss; 20 items; latest 2026-09-18T21:19:46+00:00)

- 2026-09-18T21:19:46+00:00 | As Michigan forges forward on literacy | https://example.com/post
  author: Isabel Lohman
  categories: Talent & Education; Michigan K-12 schools
  image: https://i0.wp.com/example.com/photo.jpg
  media: article
  summary: In a state laser focused on literacy, educators say more attention…
```

- Dates are normalized to ISO-8601; author comes from `dc:creator`/`atom:author`;
  `categories` from all `<category>` terms; `image` from
  `media:content`/`media:thumbnail`/`enclosure`; `summary` prefers
  `content:encoded` over `description` (tags stripped).
- `media` is `article`, `audio`, or `video`, derived from the enclosure/
  `medium` (R6) so downstream can skip podcast/CDN items. A lead image does
  **not** make an item `image`.
- Feed detection is by body (`<?xml`, `<rss`, `<feed`, `<rdf`) not just
  content-type/extension (R3).

**`meta.crawl.feeds`** mirrors the same data structurally (no bodies):

```json
{ "url": "...", "kind": "rss", "status_code": 200, "items": 20,
  "newest": "...", "oldest": "...", "media": {"article": 19, "audio": 1},
  "links": ["..."], "articles": ["..."] }
```

**Feed controls.** Feed item links are fetched as article pages directly (R2),
so articles are in the corpus without a second downstream fetch (cap
`FEED_LINK_LIMIT`). Fresh feeds suppress browser escalation for non-event
sources; event-leaning sources still escalate if their calendar looks uncovered
(R4).

> Changing the feed markdown shape changes the contract consumed by the
extraction funnel, so `extract/feeds.py` (`parse_rss_markdown`) must read the
new `author:`/`categories:`/`image:`/`media:`/`summary:` continuation lines.

## Commercial scraping API (last resort)

Off by default. When enabled, it is used **only** for sources where feeds, HTTP
(+curl_cffi) and the browser all still produce no dated content. Configured via
a git-ignored `.env` file (copy `.env.example`), env vars, or flags:

```sh
cp .env.example .env
# edit .env: SCRAPER_API_PROVIDER=scraperapi  and  SCRAPER_API_KEY=...
.venv/bin/python crawl_sources.py
```

| Setting | `.env` / env | Flag |
| --- | --- | --- |
| Provider | `SCRAPER_API_PROVIDER` | `--api {scraperapi,scrapingbee,scrapingant,generic}` |
| Key | `SCRAPER_API_KEY` | `--api-key` |
| Generic template | `SCRAPER_API_TEMPLATE` (`{url}`, `{key}`) | `--api-template` |
| JS rendering | `SCRAPER_API_RENDER=false` | `--no-api-render` |

Force a single source through the API with `engine: "api"` in `source.json`.
Without a key/template the provider is disabled automatically.

Because it is only a fallback, usage is tiny: only the handful of sources that
survive every mechanical tier.

## Scheduled collection (hourly)

`run_collect.sh` runs the full two-step pipeline and publishes the result:

1. `crawl_sources.py` — crawls only sources whose `frequency` is due.
2. `python3 -m extract` → `python3 -m extract.pipeline` → `python3 -m extract.publish`
   — builds `events.json` / `news.json` at the repo root.
3. Commits and pushes `events.json` / `news.json` when they changed.

It serializes runs (no overlap), keeps the Mac awake for the whole run, and
appends timestamped output to `data-collect/cron.log`.

On this Mac it is installed as a `launchd` agent
(`~/Library/LaunchAgents/com.dailybrief.collect.plist`, `StartInterval` 3600),
because cron does **not** fire while the Mac is asleep and launchd coalesces
missed runs on wake. `caffeinate` only prevents sleep *during* a run.

```sh
launchctl print gui/$(id -u)/com.dailybrief.collect          # status
launchctl kickstart -k gui/$(id -u)/com.dailybrief.collect   # run now
launchctl bootout gui/$(id -u)/com.dailybrief.collect        # stop hourly runs
```

- Cadence: hourly matches the shortest `frequency`; `4hours`/`daily`/`weekly`/
  `monthly` sources only run when due. With nothing due, step 1 exits
  immediately; the funnel still re-runs over the existing corpus.
- Test without publishing: `COLLECT_NO_PUSH=1 ./run_collect.sh --limit 2`.
- Review: `.venv/bin/python crawl_sources.py --status` / `--errors`; `tail -f cron.log`.
- Env knobs: `COLLECT_MAX_ITEMS` (2000), `COLLECT_EXTRACT_LIMIT` (800),
  `COLLECT_CONCURRENCY` (8), `COLLECT_OUT_DIR`, `COLLECT_NO_PUSH`.
- Incremental funnel: every triaged/extracted candidate is fingerprinted in
  `processed/extraction_index.jsonl` and never paid for again; records
  accumulate in `processed/extracted_records.jsonl`. `COLLECT_MAX_ITEMS` is a
  per-run cap, not a steady-state cost — candidates dated more than 2 days
  in the past are skipped entirely and the rest are sorted nearest-to-today
  first.
- Extraction model: `OPENROUTER_EXTRACT_MODEL` (default `qwen/qwen3-32b`); the
  previously benchmarked `qwen/qwen-2.5-72b-instruct` was retired by OpenRouter.
- cron alternative: `0 * * * * /Users/phurley/daily-brief/data-collect/run_collect.sh`.
  If cron gets `Operation not permitted`, grant `/usr/sbin/cron` Full Disk Access.

The crawler-only wrapper `run_crawler.sh` still exists for manual step-1 runs.

## Output layout

```
data-collect/
    source.json                 source catalog (input)
    crawl_log.db                SQLite log of every attempt
    crawl/<slug>/content.md     latest good combined markdown (all pages + assets)
    crawl/<slug>/meta.json      per-page/asset metadata for that run
    crawl/<slug>/html/*.html    raw HTML, only with --save-html
```

Only the **latest good result** is kept: successful files are overwritten, and a
failed run never overwrites the previous good result. `content.md` starts with
the HTML pages (shallow first), then the harvested assets, each wrapped in an
`<!-- page N -->` / `<!-- asset N -->` comment with its URL and status.

The SQLite log holds one row per attempt (time, success, status, sizes, pages,
assets, words, error); use `--prune N` to cap growth.

## Notable options

| Option | Default | Purpose |
| --- | --- | --- |
| `--max-depth N` | 3 | link depth from each seed |
| `--max-pages N` | 40 | max HTML pages per seed |
| `--max-assets N` | 30 | max PDF/ICS/feed/JSON files per source |
| `--no-assets` | off | skip non-HTML harvesting |
| `--concurrency N` | 3 | sources crawled in parallel |
| `--timeout N` | 60 | per-page/asset timeout (seconds) |
| `--save-html` | off | also store raw HTML |
| `--no-magic` / `--no-stealth` | off | disable anti-bot niceties |
| `--fail-on-error` | off | non-zero exit if any crawl failed |

## Extraction (next stage)

The crawler intentionally over-collects; `crawl/` is read-only input to the
extraction funnel. See [`EXTRACTION.md`](EXTRACTION.md) for the design. The
mechanical stages (segment, de-boilerplate, chunk) already run:

```sh
python3 -m extract                  # crawl/ -> processed/ (never writes crawl/)
python3 -m extract --stats          # stats only, write nothing
```

Output lands in `processed/<slug>/chunks.jsonl`, the input to the Jev candidate
gate (`extract/jev.py`).

Feed handling improvement proposals for the collector (preserve feed metadata,
seed article crawls from feed links, body-sniff feed detection) are in
[`CRAWLER-RECOMMENDATIONS.md`](CRAWLER-RECOMMENDATIONS.md), and the funnel-side
backlog is in [`FUNNEL-RECOMMENDATIONS.md`](FUNNEL-RECOMMENDATIONS.md).