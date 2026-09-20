# Crawler recommendations — feeds as a first-class source

Audience: `crawl_sources.py` (the collector). Goal: make RSS/Atom actually
useful downstream without a second fetch per article, while keeping
`content.md` + `meta.json` as the two-file standard.

## Why (evidence from the funnel, 72-source corpus)

- Feeds are the most reliable, dated, structured material we collect: **1,244
  kept feed items** vs 14,776 chunks, and every item has a machine date.
- But the rendered feed is lossy. `_parse_feed` (crawl_sources.py) emits only
  `date | title | link` + `description[:400]` and drops what the XML already
  contains. Bridge Michigan's raw XML, the one feed stored unrendered, shows:
  `dc:creator` ×46, `<category>` ×75, `media:content` ×40 — all discarded.
- **21% of RSS summaries sit exactly at the 400-char truncation**, 9% are empty.
- Only **14% (179/1,244)** of feed item article URLs are already present as
  crawled pages — because feed item links are not used as crawl seeds. Downstream
  enrichment would require ~1,080 extra fetches per run.
- Some feed URLs are served as `text/html`, so they are crawled as pages and the
  **raw `<item>` XML lands in `content.md`** (Bridge). The funnel now has a
  raw-XML fallback, but the collector should classify by body, not content-type.

Net: the feed is a great index that we then throw away the metadata for, and we
don't reuse its links for the HTML crawl. Fix those two things and most of the
"RSS needs a second fetch" problem disappears.

## R1 — Preserve the feed fields we already have (renderer)

Change `_parse_feed` to emit a still-markdown, still-`content.md` format that
keeps author, categories, image, media kind, and a fuller body. Proposed shape:

```
# Feed: https://example.com/feed/   (rss; 20 items; latest 2026-09-18T21:19:46Z)

- 2026-09-18T21:19:46Z | As Michigan forges forward on literacy | https://example.com/post
  author: Isabel Lohman
  categories: Talent & Education; Michigan K-12 schools
  image: https://i0.wp.com/example.com/photo.jpg
  media: article
  summary: In a state laser focused on literacy, educators say more attention needs to be paid to math. …
```

Rules:
- Normalize `pubDate`/`updated`/`published` to ISO-8601 (keep offset).
- Read `dc:creator` / `author` (RSS) and `author/name` (Atom).
- Read all `<category>` / `<category term=…>`.
- Read `media:content`/`media:thumbnail`/`enclosure` URL; set
  `media: article|image|audio|video` from `enclosure type`.
- Prefer `content:encoded` over `description`; **raise the 400-char cap** to
  ~1,500 chars (strip tags) so summaries aren't cut mid-sentence.
- Escape newlines in values so each field stays one line (keeps parsing robust).

Backward compatibility: this is a superset of today's two-line format, so a
parser that ignores unknown `key: value` lines still works. The funnel's
`extract/feeds.parse_rss_markdown` will be updated in the same change to read
`author`/`categories`/`image`/`media` and the `summary:` field.

## R2 — Seed the HTML crawl from feed item links (highest leverage)

Feeds are already fetched **first** in `Crawler.fetch`, so their item links are
available before the HTML crawl starts. Use them:

1. After `feed_pages = await self._http_many(_feed_urls_for(source), …)`, extract
   item links from the parsed feeds (do the feed parse in Python, not only to
   markdown) — call it `feed_links`.
2. Pass the top-N (cap by `max_pages`) as **additional HTML seeds**, e.g.
   `html_seeds = _dedupe_strings(html_seeds + feed_links[:max_pages])`, and/or
   boost their relevance in `SEED_KEYWORDS` scoring.
3. Set `include_external=False` already guards cross-domain; feed links are
   usually same-host.

Effect: article pages land in the corpus normally, so downstream enrichment
becomes a **URL join** instead of ~1,080 fetches, and the best-first crawl spends
its page budget on the source's actual current articles instead of nav pages.

Guardrails: cap added seeds (e.g. `min(max_pages, 20)`), keep `max_pages`
authoritative, and skip links already in the page budget.

## R3 — Classify feeds by body, not content-type

`_fetch_resource` routes on extension/`content_type`. A feed served as
`text/html` falls through to `_parse_html`, producing the raw-XML-in-markdown
case. Add a body sniff before the extension check:

```python
head = decoded.lstrip()[:512].lower()
looks_xml_feed = head.startswith("<?xml") or "<rss" in head or "<feed" in head
if looks_xml_feed or ext in (".xml", ".rss", ".atom") or "xml" in content_type:
    markdown = _parse_feed(decoded, url)
```

Also apply the same sniff in `_discover_feeds_in_pages` so a feed discovered as
a page is recognized and re-fetched/parsed as a feed rather than chunked.

## R4 — Feed-primary routing and feed hygiene

`Crawler.fetch` currently gates the browser tier on `feed_words < FEED_MIN_WORDS`
(150 words of rendered markdown). That proxy is weak. Prefer **parsed item count
and freshness**:

- Compute `feed_items` and `newest_item_age`. If `feed_items >= 3` and
  `newest_item_age <= 7d`, treat the source as **feed-covered**: skip the
  browser tier (or cut `max_pages` sharply) unless `engine == "browser"`.
- Record which path was used (`feeds`, `http`, `browser`) in `meta.crawl`.

Feed hygiene in `_feed_urls_for` / auto-feed discovery:
- Skip or down-rank `comments/feed/`, `/tag/`, `/category/` feeds when a plain
  `/feed/` or `/rss` exists. Round 2 shows comments feeds and old tag feeds are
  the main stale/junk feed items.
- Keep the current "feeds are configuration, not assets" behavior (independent
  of `--no-assets`).

## R5 — Summarize feeds in `meta.json`

Keep the two-file standard; add a compact, bounded `feeds` block to
`meta.crawl` for routing and downstream joins (no full bodies — those stay in
`content.md`):

```json
"feeds": [
  {
    "url": "https://example.com/feed/",
    "kind": "rss",
    "status_code": 200,
    "items": 20,
    "newest": "2026-09-18T21:19:46Z",
    "oldest": "2026-06-01T10:00:00Z",
    "links": ["https://example.com/post", "…"]   // capped, for R2/join
  }
]
```

This gives the funnel a reliable source of feed item URLs to join against
(`meta.crawl.pages`) without re-parsing markdown, and lets it measure feed
coverage per source.

## R6 — Mark media/enclosures so downstream can skip non-articles

WDET's feeds yield **393 kept items that are podcast/CDN URLs**
(`play.cdnstream1.com`), which can never be enriched into `news.schema` stories.
With R1's `media:` field, the funnel can route `media=audio` to a different
treatment instead of attempting an article fetch. Cheap and removes ~32% of kept
feed volume from the article-enrichment path.

## Priority

| # | Change | Effort | Impact |
| --- | --- | --- | --- |
| 1 | R2 seed crawl from feed links | M | High — removes the need for a second fetch |
| 2 | R1 preserve author/categories/image/full summary | S | High — recovers fields already fetched |
| 3 | R3 body-sniff feed detection | S | Medium — fixes raw-XML-in-content.md |
| 4 | R6 mark audio/enclosure media | S | Medium — skips podcast CDN items |
| 5 | R4 feed-primary routing + feed hygiene | S | Medium — saves crawl budget |
| 6 | R5 `meta.crawl.feeds` summary | S | Medium — enables joins/coverage metrics |

## Coordination

R1 changes the feed markdown contract, so `extract/feeds.py`
(`parse_rss_markdown`) must be updated in the same PR to read the new
`author:`/`categories:`/`image:`/`media:`/`summary:` continuation lines. R2/R3
also let the funnel's raw-XML fallback become a safety net rather than the
primary path for mis-served feeds.

## What we do NOT need

A per-article second fetch is only a fallback: for kept, self-hosted article
URLs from RSS that R2 did not manage to crawl. Do it downstream, lazily, after
the recency filter and the Jev gate — never for the ~32% of feed items that are
audio/CDN, and never for feeds that R1/R2 already cover.
