# News & Events Extraction — Design Plan

Turn the raw crawl corpus (`crawl/<slug>/content.md` + `meta.json`) into
structured, deduplicated news/event records suitable for the Daily Brief.

The collector is deliberately permissive ("too much beats too little"). This
stage is the funnel that removes the noise and emits typed records.

## Guiding principle

> Code does what code is good at. Jev does high-volume fuzzy decisions.
> The LLM does only the irreducible generation.

Never send raw markdown to a model. Every stage narrows the input; by the time
the LLM runs it should see roughly 5–10% of the corpus, and only the pages that
actually changed since the last crawl.

## Corpus facts this design is built around (measured 2026-09)

- 30 crawled sources, **1174 HTML pages + 260 assets, ~24 MB**.
- Mean 39 pages/source (range 2–70).
- **159 pages are 4xx** and **156 pages share a `sha256`** with another page —
  both are free wins before any model runs.
- Heavy nav/footer chrome: dropping lines that recur on >50% of a source's
  pages removes **~43% of the text** (22.8 MB → 12.9 MB of stripped lines).
- JSON-LD is essentially absent (1 source), so there is no schema.org shortcut.

## What Jev is (typesafe.ai)

Jev is TypeSafe AI's first **System One Model** — a *decision* model, not an
LLM. It matters here because it fills the gap between mechanical code and the
LLM:

- **Typed structured outputs, no text generation.** Output types are declared
  in advance; it cannot hallucinate free-form strings and makes no type errors.
- **Parallel sampler:** 70–500 ms end-to-end; **$0.042 / MTok input, output
  free** (claims: 193× faster / 444× cheaper than LLMs).
- **Calibrated confidence** on every output (trained with RLCD, not RLHF).
  High confidence → act autonomously; low confidence → escalate/review.
- Built for "smart if-statements": classify, route, score, extract-into-enums,
  verify, branch. Not built for writing titles/summaries.

Early access; docs are behind sign-in. The design keeps the Jev tier behind a
small interface so a cheap LLM can stand in for it until access lands — you
just lose guaranteed type-safety and honest confidence.

## The funnel

```
                       input: crawl/<slug>/{content.md,meta.json}
  ┌───────────────────────────────────────────────────────────────────┐
  │ 0. Change detection    [mechanical]  key pages by sha256; skip     │
  │                                       unchanged / 4xx / dupes      │
  │ 1. Segment + class     [mechanical]  split <!-- page N -->, URL     │
  │                                       rules → url_class            │
  │ 2. De-boilerplate      [mechanical]  drop cross-page recurring      │
  │                                       lines / nav link lists        │
  │ 3. Chunk               [mechanical]  heading-aware blocks with      │
  │                                       provenance + candidate hints │
  ├───────────────────────────────────────────────────────────────────┤
  │ 4. Candidate gate      [JEV]         is_content / kind / dated /    │
  │                                       timeframe / relevance + conf  │
  │ 5. Dedupe              [mech + JEV]  URL canon + simhash; Jev       │
  │                                       "same item?" pair tiebreak    │
  │ 6. Router              [mechanical]  (source.type,url_class,kind)   │
  │                                       → schema/prompt/model tier    │
  │ 7. Extraction          [LLM]         structured JSON: title,        │
  │                                       summary, dates, venue, url    │
  │ 8. Validate            [mech + JEV]  schema, TZ, sanity, Jev        │
  │                                       verifier "matches source?"    │
  │ 9. Store               [mechanical]  idempotent upsert by stable id │
  └───────────────────────────────────────────────────────────────────┘
                       output: records + review queue
```

### 0. Change detection (mechanical)
`meta.json` already carries per-page `sha256`, `status_code`, `success`.
Extraction is keyed by `(source_slug, page_sha256, funnel_version)`: a page
whose hash is unchanged since the last run is skipped entirely. Assets get the
same treatment. This is the single biggest cost lever because crawls run
hourly/daily.

### 1. Segment + URL class (mechanical)
Split `content.md` on `<!-- page N -->` / `<!-- asset N -->` markers and join to
`meta.crawl.pages` by `(kind, index)`. Drop pages that failed or returned 4xx,
and collapse `sha256` duplicates (keep the shallowest, i.e. lowest `depth`).

Classify each URL by path rules (`/events`, `/event`, `/news`, `/blog`,
`/calendar`, `.pdf`, feed, `/wp-content`, …) into a small `url_class` vocab.
The observed first-URL-segments (`/events` 37, `/event` 35, `/article` 30,
`/2026` 46, `/news` 12, …) show this is already a decent signal.

### 2. De-boilerplate (mechanical)
Within a source, count normalized line frequency across pages. Drop lines that
appear on more than `--boilerplate-threshold` (default 0.5) of pages, with a
minimum page count guard so a 2-page source isn't over-pruned. Also collapse
long pure-link lists (nav menus) and repeated markdown image-only lines.
Measured effect: ~43% text reduction, before any semantic filtering.

### 3. Chunk + parse feeds (mechanical)
Heading-aware chunking: start a new chunk at a markdown heading or when the
current block exceeds `--max-tokens`; merge blocks below `--min-tokens`; keep
`--overlap` tokens of tail context. Every chunk carries full provenance
(`source_slug, page_index, url, page_sha256, page_title, heading, char_start/end`)
plus mechanical **candidate hints** (dates found, event/news keywords,
`url_class`, asset kind) so stage 4 has cheap features to lean on.

**Feeds bypass chunking.** Round-1 labeling showed RSS/Atom entries already
carry most of a `news.schema.json` story. `extract/feeds.py` reverses the
crawler's feed rendering back into structured records:

- RSS/Atom → `target=news` (`title`, `url`, `publishedAt` ISO-8601, `summary`);
- ICS → `target=event` (`title`, `start`, `end`, `venue`, `summary`).

These land in `processed/<slug>/feed_items.jsonl` and are excluded from
`chunks.jsonl` by default (override with `--keep-feed-chunks`). The parser is
network-free — it reads only the already-crawled markdown. It also handles
**raw RSS/Atom XML**: some feed URLs are served as `text/html` (and crawled as
pages), so the unrendered `<item>`/`<entry>` XML reaches `content.md`; the
fallback parser recovers those items instead of chunking the XML as text.
Found in round 2 on Bridge Michigan (see `gold/round2/FINDINGS.md`). Collector
changes that would remove the need for a per-article second fetch are proposed
in [`CRAWLER-RECOMMENDATIONS.md`](CRAWLER-RECOMMENDATIONS.md).

**Dates are normalized.** `extract/dates.py` turns every absolute date found in
a chunk into ISO-8601. Dates with an explicit offset keep it; **dates without a
timezone are assumed to be US Eastern (`America/Detroit`), DST-aware.** Dates
without a year use a context year from the page URL/title when one is available
(else the reference year) and are flagged `year_assumed`. Chunk signals gain
`normalized_dates`, `date_spans`, `date_recency` (`past`/`today`/`future`/
`mixed`/`none`), `has_future_date`, `nearest_days_from_ref`, and
`year_assumed_count`; feed items gain `recency` and `age_days`. Recency is
computed against `--reference-date` (default: today Eastern).

**Locality is inferred per item.** `extract/locality.py` adds a `locality`
signal to every chunk and feed item — `{places, tier, out_of_area, confidence,
reason}` — using a coverage gazetteer plus aggregator URL state slugs. This is
what catches a Canton-labelled Patch feed returning Illinois/Georgia items.
`--drop-out-of-area` (opt-in; default keeps) drops flagged records.

**Feed items join to crawled pages.** `extract/urls.canonical_url` indexes
crawled pages and each feed item gains `article_crawled` / `article_page_index`,
so enrichment can reuse a page instead of fetching it.

### 4. Recency filter (mechanical)
A two-tier filter drops stale dated records before the Jev gate:

- **Known date** — a date found in content that probably marks a real event
  (e.g. "Concert on Sept 30"): dropped if more than **`--max-age-days` (14)** old.
- **Publish / ambiguous date** — e.g. an RSS `pubDate`, or a year-less date:
  kept up to **`--publish-max-age-days` (183, ~6 months)**.
- **Future dates are always kept**, and undated records are kept (nothing to
  judge on).

Bias is *when in doubt, keep more*: only unambiguous known event dates (role
`event` **and** explicit year) use the strict window; anything ambiguous falls
back to the lenient one. Filtered records are not lost — they go to
`chunks_dropped.jsonl` / `feed_items_dropped.jsonl` with a `filter_reason`.
Disable entirely with `--no-filter`.

`extract/funnel.py` implements stages 0–3 and writes `processed/<slug>/chunks.jsonl`.
This is the input contract for the Jev gate. Chunk `char_start`/`char_end` are
offsets into the *cleaned* page text; `page_char_offset` gives the raw offset of
that page's block within `content.md` for drill-down provenance.

### 4. Candidate gate (Jev)
One task per chunk, entirely enum/bool/number outputs:

```
is_content   : bool
kind         : enum[event, news, notice, agenda, listing, nav, other]
dated        : bool
timeframe    : enum[past, today, future, recurring, undated]
relevance    : 0..4
```

Use confidence to route:
- high-confidence `nav`/`other` → drop;
- high-confidence `event`/`news`/`notice`/`agenda` → extraction queue;
- low confidence → LLM triage tier (or review queue), never a guess.

At $0.042/MTok this can run on every changed chunk on every run.

### 5. Dedupe (mechanical + Jev)
**Implemented** (`extract/dedupe.py`), run as a post-pass over each stream.
Signals: exact `content_hash` → exact long normalized title → SimHash over token
shingles with banded LSH (accepted only when titles are also similar). Only
content-bearing chunks (`candidate_hint` event/news/notice/agenda) are eligible,
and groups larger than 15 are treated as generic and left unmerged. Records gain
`dup_group` / `is_canonical` / `duplicate_of`; the canonical pick prefers
in-window, then most local, then longest. A Jev `same_item?` tiebreak can be
added later for the borderline pairs. Downstream stages skip non-canonical
records. `--no-dedupe` disables; `--dedupe-threshold` tunes it.

### 6. Router (mechanical)
A deterministic table, **not** a model:
`(source.type, url_class, jev.kind)` → `{output_schema, prompt_template, model_tier, max_tokens}`.
Only escalate to a model-based routing decision when Jev confidence is low.
OpenRouter tiers: cheap flash/mini for triage, mid for standard extraction,
strong only for roundups / multi-item pages.

### 7. Extraction (OpenRouter LLM)
The only stage that generates free text (title, summary) — the thing Jev
deliberately gives up. Force JSON-schema structured outputs:

```json
{
  "kind": "event|news|notice|agenda",
  "title": "string",
  "summary": "string",
  "start": "ISO-8601 or null",
  "end": "ISO-8601 or null",
  "all_day": false,
  "venue": "string or null",
  "address": "string or null",
  "city": "string or null",
  "locality_index": 0,
  "url": "string",
  "tags": ["string"]
}
```

Route by `source.type`: venue → event schema; news outlet → article schema;
government → notice/agenda schema.

### 8. Validate / reconcile (mechanical + Jev)
- JSON schema validation; ISO-8601 normalized to `America/Detroit`.
- Reject dates outside a sane window; URL must belong to the source domain.
- Venue gazetteer matching.
- Jev verifier: "does this record faithfully match the source chunk?"
  (bool + confidence). Below threshold → review queue, not the brief.

### 9. Store (mechanical)
SQLite (or JSONL) keyed by a stable id:
`sha1(source_slug | url | normalized_title | normalized_start)`.
Upsert on re-crawl. Persist Jev confidence, model/prompt version, and full
provenance. Publish gate = validator pass × calibrated confidence.

## Repository layout

`crawl/` is **read-only input**. The funnel never writes into it; all output
lands in a parallel `processed/` tree (and the code refuses to run if the
output path resolves inside `crawl/`).

```
data-collect/
  EXTRACTION.md            this plan
  crawl/<slug>/            INPUT ONLY (crawler owns this tree)
    content.md
    meta.json
  extract/
    __init__.py
    funnel.py              stages 0–3 (mechanical), CLI
    jev.py                 stage 4 contract: task vocab + request/response
                           schema + pluggable runner (stub until access lands)
    __main__.py            python -m extract
  processed/               OUTPUT (funnel owns this tree)
    <slug>/
      chunks.jsonl         in-window chunk records (input to stage 4)
      chunks_dropped.jsonl date-filtered-out chunks (audit)
      feed_items.jsonl     in-window RSS/Atom/ICS records
      feed_items_dropped.jsonl
      funnel.json          per-source stats + input fingerprint
      pages.jsonl          cleaned page text (only with --emit-pages)
    summary.json           aggregate run stats
    .state.json            per-source fingerprints for --changed-only
```

Contract: `crawl/` is treated as immutable. If the funnel needs to persist
anything, it goes under `processed/` (override with `--out`).

## Running the funnel (first pass)

Run from `data-collect/`; no extra dependencies (standard library only):

```sh
python3 -m extract                              # all sources -> processed/
python3 -m extract --source <slug> --emit-pages # one source, keep cleaned pages
python3 -m extract --changed-only               # skip sources whose input is unchanged
python3 -m extract --stats                      # compute stats, write nothing
python3 -m extract --list-sources               # show crawl slugs
python3 -m extract.jev                          # live Jev gate smoke test (uses .env key)
python3 -m extract.characterize --task v7 --score  # characterization experiments
python3 -m extract.score_gate gold/round2 --sweep  # gate precision/recall on gold
python3 -m extract.extraction --gold gold/round2 --n 6  # triage -> route -> extract
python3 -m unittest extract.test_jev            # offline interface tests
```

Key knobs: `--boilerplate-threshold` (default 0.5), `--target-tokens` /
`--max-tokens` / `--min-tokens`, `--no-assets`, `--keep-4xx`,
`--keep-feed-chunks` (feeds are parsed structurally by default),
`--reference-date YYYY-MM-DD` (recency reference; default today Eastern),
`--max-age-days 14` / `--publish-max-age-days 183` (recency windows),
`--no-filter` (keep all dated records), `--drop-out-of-area` (drop locality
flags; default keeps them), `--no-dedupe` / `--dedupe-threshold 3`.

`crawl/` is never written to. The output tree defaults to `processed/`; the CLI
refuses to run if `--out` resolves inside `crawl/`.

### Stage-4 gate (Jev)

The candidate gate contract lives in `extract/jev.py` and runs offline against
the first pass today:

```python
from extract.jev import StubJevClient, gate_chunks, load_chunks
chunks = load_chunks("processed/<slug>")
result = gate_chunks(chunks, StubJevClient())   # accepted / dropped / escalated
```

Replace `StubJevClient` with a real `JevClient` once early access lands; routing
uses calibrated confidence over `is_content`, `kind`, and `relevance`
(`ROUTING_FIELDS`) so an uncertain `timeframe` doesn't force escalation.

## Status (first pass)

Implemented, standard-library only:

- Stages 0–3, runnable via `python3 -m extract`.
- URL classification, generator-header stripping, cross-page boilerplate
  removal, heading-aware chunking, candidate hints, change detection, and the
  read-only-input guard.
- `extract/feeds.py`: network-free RSS/Atom/ICS parser producing
  `feed_items.jsonl` (feeds are not chunked by default).
- `extract/dates.py`: date normalizer (Eastern-assumed, DST-aware) feeding
  recency signals into chunks and feed items.
- Two-tier recency filter (14-day known event / 183-day publish) writing
  `*_dropped.jsonl` for audit.
- Per-item locality detection (`extract/locality.py`) and feed→page join
  (`extract/urls.py`), with an opt-in `--drop-out-of-area`.
- Cross-source dedupe (`extract/dedupe.py`) marking `is_canonical`.
- `extract/jev.py`: the stage-4 task vocabulary, chunk→state projection, the
  one-call `TRIAGE` task + `triage_record()`, `OpenRouterJevClient` /
  `TypeSafeJevClient`, and a deterministic `StubJevClient`.
- `extract/pipeline.py` (full run) and `extract/publish.py` (stage 8/9):
  `publish` normalizes + validates each record against the root schemas and
  writes schema-shaped `events.json` (v2.0.0, single `events` array) and
  `news.json` (v1.1.0) — it **refuses to write** if the document fails
  validation. Records carry `imageUrl`/`imageAlt` (events) and `photo`
  (stories), joined from the crawler's per-page image / feed image.
- Event scoring rides along in the triage call (`extract/scoring.py`), 0–100
  from nine positive / four negative Nouls; see `gold/round2/SCORING.md`. The
  events schema is now a single un-capped `events` array
  (`schemas/events.schema.json`, v2.0.0).
- `extract/score_gate.py` / `extract/characterize.py`: evaluate gate and
  characterization against a labeled gold set.

Corpus result on the current crawl (72 sources, `--reference-date 2026-09-19`):
**17,838 chunks → 14,776 kept / 3,062 filtered**; **3,948 feed items → 1,244 kept
/ 2,704 filtered** (WDET's archive). Kept-chunk breakdown: ~8.9k undated,
~2k future, ~2k publish/ambiguous within 6 months, ~0.7k known events within
2 weeks; 245 4xx and 54 duplicate pages dropped.

Not yet built (next passes):

- Real Jev client + calibrated thresholds tuned on a gold set.
- Dedupe (stage 5), the deterministic router (6), LLM extraction (7),
  validation (8), and the idempotent store (9).


## Gold set

Hand-labeled samples live in `gold/` (tracked; labels are not reproducible).
Each round records its seed and corpus fingerprint so rounds are comparable.

```sh
python3 -m extract.sample --n 80 --seed 5 --per-source-cap 2 \
    --out gold/round1/candidates.jsonl
# label into gold/round1/labels.jsonl, then:
python3 -m extract.score_gold gold/round1/labels.jsonl
```

**Round 1** (80 chunks, 51-source corpus) is complete; see
`gold/round1/FINDINGS.md`. Headline results: only 21% of chunks are actionable
now, mechanical `hint` precision is ~0.4–0.5, feeds are the most mechanical
source, documents/homepages are mostly noise, and staleness/locality dominate.

**Round 2** runs after full collection (~75 sources) with a fresh seed and a
larger sample; compare via `score_gold`. Open `gold/README.md` for procedure.

## Suggested order of work

1. ~~**Funnel (this pass)** — stages 0–3, runnable, inspectable output.~~ **done**
2. **Gold set** — hand-label ~100 chunks; use it to tune Jev thresholds and the
   router. Calibrated confidence only helps if you measure it.
3. **Jev gate** — wire the real API once off the waitlist; until then run the
   stub/cheap-LLM stand-in behind the same interface.
4. **Dedupe + router + extraction** — add the OpenRouter tier.
5. **Validation + store** — publish gate and idempotent upsert.