# Crawler bug: per-article feed discovery

**Status: FIXED** in `crawl_sources.py` (`_is_site_level_feed` /
`_site_level_feeds`, depth-0 discovery). Verified live on the two worst sources:
`auto_feeds` went from 30/26 per-article feeds to **2 site feeds each**, dead
feed fetches **30+26 → 0**, and `feed_items` still populate (26 items across the
two sources).

**Severity:** low–medium (wasted budget, noisy output; no data loss)
**Component:** `crawl_sources.py` — `_discover_feeds()` / `_discover_feeds_in_pages()`
**Found:** while re-running the extraction pipeline on the 74-source crawl
(2026-09-19).

## Symptom

`meta.crawl.auto_feeds` contains one feed URL **per article** in addition to the
site feed. On the latest run: **362 per-article feed URLs vs 48 site-level**.
Most of them are fetched and return **410 Gone**.

Example (`crawl/the-communicator-ann-arbor-community-high-school/meta.json`):

```
https://chscommunicator.com/feed/                     <- keep
https://chscommunicator.com/feed/atom/                <- keep
https://chscommunicator.com/113459/features/2026/09/snippets-of-summer/feed/
https://chscommunicator.com/113469/opinion/2026/09/how-deep-are-cultural-differences/feed/
https://chscommunicator.com/113397/photo-of-the-day/2026/09/september-18-2026/feed/
... (30 per-article feeds on this source alone)
```

Dead-feed fetch counts in the latest run:

| source | status | count |
| --- | --- | --- |
| the-communicator-ann-arbor-community-high-school | 410 | 30 |
| the-huron-emery-ann-arbor-huron-high-school | 410 | 26 |
| the-schoolcraft-connection-schoolcraft-college | 410 | 25 |
| the-perspective-p-cep-student-newspaper | 410 | 23 |
| the-skyline-post-ann-arbor-skyline-high-school | 410 | 1 |
| cliff-bell-s | 404 | 1 |
| university-of-michigan-museum-of-natural-history | 500/404 | 2 |

## Root cause

WordPress emits a `<link rel="alternate" type="application/rss+xml">` for the
**per-post** feed (and per-post comments feed) on every article page, not just
the site feed. `_discover_feeds` collects every such link from every crawled
page, so article pages contribute their own feed URLs. Because the article feed
endpoints are gone/disabled (410) or disallow it, the fetches are wasted.

## Impact

- Wastes the per-source `--max-assets` budget and crawl time on dead requests
  (105 dead feed fetches across 4 student-newspaper sources this run).
- Pollutes `content.md` / `meta.json` with dozens of junk feed entries.
- A per-article feed, if it did resolve, would duplicate that one article's
  content — not a useful source-level feed.

## Repro

```sh
jq -r '.crawl.auto_feeds[]' crawl/the-communicator-ann-arbor-community-high-school/meta.json \
  | grep -c '/20..\/..\/.*\/feed/$'     # -> 30
```

## Suggested fix

Filter discovered feeds to **site-level** feeds before fetching:

1. Keep feed paths with at most one path segment before the feed token —
   `/feed`, `/feed/`, `/feed/atom`, `/rss`, `/rss.xml`, `/atom.xml`, `/blog/feed`,
   `/news/feed` — and drop anything where the path looks like an article
   (≥2 path segments before the terminal `feed`, or a date / numeric-ID segment).
2. Dedupe by host and cap to a small number (e.g. 3) of feeds per source.
3. Only run feed discovery on the **seed/home** page (and maybe section pages),
   not on every crawled article.

## Fix applied

- `_is_site_level_feed(url)` keeps only feeds with ≤1 path segment before the
  feed token (`/feed`, `/feed/atom`, `/rss`, `/rss.xml`, `/blog/feed`,
  `/news/feed`, `?feed=rss2`) and rejects any numeric/date prefix segment
  (`/113459/features/2026/09/…/feed`).
- `_site_level_feeds(urls, per_host=3)` filters and caps feeds per host,
  preferring shorter paths.
- `_discover_feeds_in_pages` now runs discovery on **depth-0 (seed/home) pages
  only**, not every crawled page.

```python
_FEED_TAILS = {"feed", "rss", "atom", "rss.xml", "atom.xml", "feed.xml", "index.xml"}

def _is_site_level_feed(url): ...   # <=1 prefix segment, no numeric/date segment
```

## Acceptance criteria — met

- The four student-paper sources now expose only site-level feeds (2 each here).
- Dead feed fetches dropped to 0 on the re-crawl.
- Site feeds still populate `feed_items` (no regression).

- `auto_feeds` contains only site-level feeds for the four student-paper sources
  above (≤ 3 each, no `/20../..//feed/` paths).
- Dead feed fetches (4xx/410 on feed URLs) drop to ~0 on a full run.
- Site-level feeds still parse and populate `feed_items` (no regression).

---

## Not a crawler bug (for the record)

The extraction failures I first hit were **funnel-side**, not scrape-side:
`processed/<slug>/feed_items.jsonl` records carry `title`/`summary`/`venue`/
`author`/`categories` but no `text`, and the funnel's `chunk_state` /
extraction prompt assumed every record had `text`, so feed items were sent to
the models as an empty state. Fixed in `extract/jev.py` by adding
`record_text()` (used by `chunk_state` and `extract/prompts.build_messages`).
The scraper's feed output itself is correct — 215 feed blocks, **0** count
mismatches between the header and the items, 0 items missing title/link/date,
no raw XML remaining.