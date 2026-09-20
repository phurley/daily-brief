# Funnel recommendations — what to build next

Audience: `extract/` (the funnel). Complements
[`CRAWLER-RECOMMENDATIONS.md`](CRAWLER-RECOMMENDATIONS.md); items marked
**[crawler-dep]** pair with a collector change, the rest are independent.

## Where the funnel is today

Built and running:

- Stages 0–3: change detection, segmentation + URL classes, de-boilerplate,
  heading-aware chunking.
- `extract/feeds.py`: RSS/Atom/ICS parser (incl. raw-XML fallback) →
  `feed_items.jsonl`.
- `extract/dates.py`: Eastern-assumed, DST-aware normalization + two-tier
  recency filter (14-day known event / 183-day publish), future always kept.
- `extract/jev.py`: stage-4 task contract + router + deterministic stub.
- Gold rounds 1–2, sampler, scorer.

Not built: dedupe (5), router (6), extraction (7), validation (8), store (9).

## Immediate, crawler-independent

### F1 — Per-item locality detection — **done** (`extract/locality.py`)
Round 2's top false-positive source: Patch (IL/GA) and Sun Times (IL) return
*real, current* events that are simply out of area. `source.locality_index`
cannot be trusted per item.

`extract/locality.py` emits a `locality` signal on every chunk and feed item:
`{places, tier, out_of_area, confidence, reason}`. It uses a coverage gazetteer
(tiers 0–4), aggregator URL state slugs (strong), and non-Michigan state
mentions (weak; a co-occurring "Michigan" makes them ambiguous → keep).

First full run: **1,278 out-of-area chunks / 97 out-of-area feed items** — and
`canton-focus-local-patch-canton` is **98% out-of-area** (836/836 chunks),
i.e. that source is effectively a national Patch feed. `--drop-out-of-area`
(opt-in; default keeps) drops them to 20 chunks.

### F2 — Join feed items to crawled pages — **done**
`extract/urls.canonical_url` (strips `www.`, tracking params, fragments,
trailing slashes) now indexes crawled pages by URL, and each feed item gets
`article_crawled` / `article_page_index` / `article_page_sha256`.

Band-aid result: join rate rose **14% → 20%** (179 → 250 of 1,244 kept items)
by fixing tracking-param mismatches. The rest needs crawler R2 (feed item URLs
as crawl seeds). `funnel.json` reports `feed_items_joined`.

### F3 — Feed enrichment fallback (stage 6)
For kept feed items that are article-like (`media != audio`), self-hosted, and
**not** joined in F2, do a bounded second fetch of the article and merge into the
`news` shape (full summary, author, photo, locations, topics). Only for items
that survive the recency filter and Jev gate — never for the ~32% of feed items
that are podcast/CDN. Behind a flag until the crawler's R2 lands.

### F4 — Undated policy
47/100 kept chunks in round 2 were undated, mostly nav/other. The filter
deliberately keeps them ("when in doubt, keep more"), so this is a Jev-gate job.
Decision needed: leave as-is and let the gate drop them, or require a weak
content signal (heading + non-link text + length) before they reach the gate.
Recommend leaving as-is for now and measuring drop rate in round 3.

### F5 — Dedupe (stage 5) — **done** (`extract/dedupe.py`)
Sources overlap heavily (township ↔ library ↔ Patch ↔ Focus all carry the same
Canton items; feeds repeat deep-crawl pages). A post-pass clusters records and
marks `dup_group` / `is_canonical` / `duplicate_of`.

Signals: exact `content_hash` → exact long normalized title → SimHash over token
shingles with banded LSH (accepted only when titles are also similar). Guards:
only content-bearing chunks (`candidate_hint` event/news/notice/agenda) are
eligible, and groups larger than 15 are treated as generic and left alone. The
canonical pick prefers in-window, then most local, then longest text.

First validated run: **3,084 chunk duplicates (largest group 15)** and 19 feed
duplicates; ~16s. `processed/dedupe.json` records the totals; downstream stages
skip records where `is_canonical` is false. `--no-dedupe` disables the pass;
`--dedupe-threshold` tunes the SimHash distance.

## Depends on crawler changes

### F6 — Parse enriched feed markdown — **done**
The crawler now renders feeds as `# Feed: URL   (rss; N items; latest ISO)` with
`author:` / `categories:` / `image:` / `media:` / `summary:` lines.
`extract/feeds.parse_rss_markdown` reads them, so feed items carry author,
categories, image, media kind, and the full summary (no more 400-char cap).
Regression found and fixed while re-running: feed items have no `text` key, so
`jev.chunk_state` / the extraction prompt were sending empty states — both now
use `jev.record_text()`.

### F7 — Raw-XML fallback becomes a safety net **[crawler-dep]**
Once crawler R3 body-sniffs feeds, the `feed_kind == "raw"` path should rarely
trigger. Keep it; add a counter in `funnel.json` to alert if it grows.

## Later stages

- **Router (6): done** — `extract/router.py`. Deterministic table from
  `cardinality` × `attend_on_date` → `skip` / `event_single` /
  `event_array` / `news_single` / `news_array` (array caps 5 / 12).
- **Jev gate: done** — `extract/jev.py` ships `OpenRouterJevClient`
  (`/api/alpha/decisions`, `typesafe/jev-1.13`), `TypeSafeJevClient`, and the
  one-call `TRIAGE` task + `triage_record()`. Gold: accept P0.92 / R0.79,
  router acc 0.68. See `gold/round2/CHARACTERIZATION-REPORT.md`.
- **Extraction (7): done (first pass)** — `extract/prompts.py` (event/news ×
  single/array JSON schemas + prompts), `extract/llm.py` (OpenRouter chat,
  structured output), `extract/extraction.py` (triage → route → extract), and
  `extract/bench.py` (model comparison). Benchmark: all models 100% schema;
  date accuracy is the differentiator; default is `qwen/qwen-2.5-72b-instruct`.
  See `gold/round2/MODEL-BENCH.md`.
- **Validation (8) → store (9):** pending — assign ids/addedAt/localityIndex,
  enforce `events.schema.json` / `news.schema.json`, ISO Eastern dates,
  venue/date sanity, idempotent upsert.

## Recommended order

1. ~~**F1 locality** and **F2 feed join**~~ **done** — both target round-2's
   findings and now feed the coverage metrics the crawler work needs.
2. ~~**F5 dedupe**~~ **done** — `extract/dedupe.py`, run as a post-pass.
3. **F6/F7** as the crawler changes land.
4. Stages 6–9.

## Round-3 gold watch-list

- Locality: how much out-of-area volume F1 removes, and its precision on the
  round-2 Patch/Sun Times cases.
- Feed coverage: does F2 (plus crawler R2) lift the 14% join rate?
- Undated drop rate: what fraction of the 47% undated kept chunks the Jev gate
  rejects.
