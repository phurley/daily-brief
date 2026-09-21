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
  │ 5. Dedupe              [mechanical]  content_hash + title + simhash │
  │                                       clustering; marks is_canonical│
  │ 6. Router              [mechanical]  (source.type,url_class,kind)   │
  │                                       → schema/prompt/model tier    │
  │ 7. Extraction          [LLM]         structured JSON: title,        │
  │                                       summary, dates, venue, url    │
  │ 8. Validate            [mechanical]  schema (per-record + per-doc), │
  │                                       TZ, sanity; refuse bad write  │
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
Found in round 2 on Bridge Michigan (see the local round-2 gold report under
`gold/round2/`). Collector changes that would remove the need for a per-article
second fetch are implemented; see
[`CRAWLER-RECOMMENDATIONS.md`](CRAWLER-RECOMMENDATIONS.md).

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

### 4a. Recency filter (mechanical)
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

### 4b. Candidate gate (Jev)
One task per chunk, entirely enum/bool/number outputs:

```
is_content   : bool
kind         : enum[event, news, notice, agenda, listing, nav, other]
dated        : bool
timeframe    : enum[past, today, future, recurring, undated]
relevance    : 0..4
is_listicle  : bool
is_lottery   : bool
is_sports    : bool
```

Use confidence to route:
- high-confidence `nav`/`other` → drop;
- **`is_listicle` → drop** at `LISTICLE_DROP_CONFIDENCE` (0.60);
- **`is_lottery` → drop** (lottery/lotto draws, winning numbers, jackpots) at
  `LOTTERY_DROP_CONFIDENCE` (0.60);
- **`is_sports` → drop** (games, teams, scores, standings, fixtures) at
  `SPORTS_DROP_CONFIDENCE` (0.60);
- high-confidence `event`/`news`/`notice`/`agenda` → extraction queue;
- low confidence → LLM triage tier (or review queue), never a guess.

The three content-class drops are confident-only: an uncertain flag keeps the
candidate. `is_listicle` was calibrated on gold round 2 (precision 1.00 at
conf ≥ 0.60, zero good candidates dropped); `is_lottery`/`is_sports` use the
same 0.60 floor until a labeled round tunes them.

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

### 8. Validate / reconcile (mechanical) — **implemented** in `extract/publish.py`
- JSON schema validation; ISO-8601 normalized to `America/Detroit`.
- Reject records missing required fields or with dates outside the publish
  window: an event is dropped when it is over — its end date passed before
  the edition day — or, start-only, when the start is older than 7 days.
- URL/image provenance is carried from the crawler/feed metadata.
- A Jev "does this record faithfully match the source chunk?" verifier remains a
  possible later refinement; the current publish gate is mechanical schema
  validation (per record and per document).

### 9. Store (mechanical) — **implemented** in `extract/pipeline.py`
JSONL keyed by a stable id:
`slug(title)-kind-YYYY-MM-DD` (see `finalize()`).
Records accumulate in `processed/extracted_records.jsonl`, merged by id on
re-crawl. Full provenance is carried under `origin` (source, chunk id, url),
and the event score / images are attached. Candidates already paid for are
tracked by fingerprint in `processed/extraction_index.jsonl`.
Publish gate = schema validation (per record and per document).

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
    __main__.py            python -m extract
    funnel.py              stages 0–3 + recency filter (mechanical), CLI
    feeds.py               network-free RSS/Atom/ICS/raw-XML parser
    dates.py               Eastern-assumed, DST-aware date normalization
    locality.py            per-item coverage/locality detection
    urls.py                canonical URL index + feed→page join
    dedupe.py              stage 5: content_hash/title/SimHash clustering
    jev.py                 stage 4 contract: task vocab + clients + gate
    router.py              stage 6: deterministic triage → extraction route
    prompts.py             event/news × single/array prompts + JSON schemas
    llm.py                 OpenRouter chat client (structured outputs)
    extraction.py          stage 7 runner (triage → route → extract)
    enrich.py              bounded event fallback; best strategy learned per
                          source (ical / JSON-LD Event parse / URL date /
                          touch-up model), cached in enrich_strategies.json
    scoring.py             event score 0–100 from triage Noul answers
    pipeline.py            incremental triage→extract→store (stages 4–9)
    publish.py             stage 8/9: validate → events.json / news.json
    sample.py              gold-set sampler
    score_gate.py          gate precision/recall against gold
    score_gold.py          gold label scoring
    characterize.py        characterization experiments
    bench.py               model comparison
    test_jev.py            offline interface tests
  processed/               OUTPUT (funnel/pipeline own this tree)
    <slug>/
      chunks.jsonl         in-window chunk records (input to stage 4)
      chunks_dropped.jsonl date-filtered-out chunks (audit)
      feed_items.jsonl     in-window RSS/Atom/ICS records
      feed_items_dropped.jsonl
      funnel.json          per-source stats + input fingerprint
      pages.jsonl          cleaned page text (only with --emit-pages)
    extraction_index.jsonl     fingerprints of candidates already paid for
    extracted_records.jsonl    cumulative record store (input to publish)
    dedupe.json                duplicate totals
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
  `feed_items.jsonl` (feeds are not chunked by default). Schema.org **JSON-LD
  `Event` blocks** (rendered by the crawler as `# Structured data (JSON-LD)`
  pages) are parsed the same way: `startDate`/`endDate`/venue/url become
  structured event items with authoritative dates, like ICS.
- `extract/dates.py`: date normalizer (Eastern-assumed, DST-aware) feeding
  recency signals into chunks and feed items.
- Forward-looking recency filter (drop events that are over or whose
  start-only date is older than 7 days; publish dates get 183 days), writing
  `*_dropped.jsonl` for audit.
- Per-item locality detection (`extract/locality.py`) and feed→page join
  (`extract/urls.py`), with an opt-in `--drop-out-of-area`.
- Cross-source dedupe (`extract/dedupe.py`) marking `is_canonical`.
- `extract/jev.py`: the stage-4 task vocabulary, chunk→state projection, the
  one-call `TRIAGE` task + `triage_record()`, `OpenRouterJevClient` /
  `TypeSafeJevClient`, and a deterministic `StubJevClient`.
- `extract/pipeline.py` (full run) and `extract/publish.py` (stage 8/9):
  the pipeline is **incremental** — every triaged/extracted candidate is
  fingerprinted (`source_slug` : `EXTRACTOR_VERSION` : `chunk_id`/`item_id`)
  into `processed/extraction_index.jsonl` and never re-paid for; records
  accumulate in the cumulative store `processed/extracted_records.jsonl`
  (merged by id, pruned at 183 days), which is what `publish` reads.
  Selection is date-sorted (nearest-to-today first) and skips candidates whose
  newest known date is more than 2 days past (`--stale-days`).
  `publish` normalizes + validates each record against the root schemas and
  writes schema-shaped `events.json` (v2.0.0, single `events` array) and
  `news.json` (v1.1.0) — it **refuses to write** if the document fails
  validation. Records carry `imageUrl`/`imageAlt` (events) and `photo`
  (stories), joined from the crawler's per-page image / feed image.
- Event scoring rides along in the triage call (`extract/scoring.py`), 0–100
  from seven positive / five negative Nouls; see the local
  `gold/round2/SCORING.md`. The events schema is now a single un-capped
  `events` array (`schemas/events.schema.json`, v2.0.0).
- The per-question Noul probabilities behind the score are persisted on every
  record and published as an optional `scoring` block (schema: `$defs/scoring`)
  — `{score?, signals:{question: P(yes)}}` — on both events and stories, so the
  weights in `extract/scoring.py` can be re-tuned from real data. Publish also
  drops any record whose stored `contentFlags` mark it as **lottery** or
  **sports** at conf ≥ 0.60, a durable backstop for records that predate the
  gate rules.
- `extract/bench.py` / `bench_live.py` (live-corpus variant): evaluate gate and
  extraction models against labeled gold or a live sample.

  Live model bench (2026-09-20, 12 mixed-mode candidates, judged by
  gpt-4.1-mini) — first-try schema validity, cost/call, p50 latency, judge
  score, key-field accuracy, fabrication rate:

  | model | sch% | $/call | p50 | judge | key_ok | fab |
  |---|---|---|---|---|---|---|
  | **qwen/qwen3-32b (default)** | 100% | 0.00056 | 20.8s | 4.25 | **92%** | **0%** |
  | google/gemini-3.1-flash-lite | 100% | 0.00116 | 2.0s | 4.17 | 83% | 8% |
  | google/gemini-2.5-flash | 100% | 0.00185 | 2.5s | 4.42 | 75% | 8% |
  | google/gemini-2.5-flash-lite | 100% | 0.00035 | 1.5s | 4.33 | 75% | 25% |
  | deepseek/deepseek-v4.1-flash | 100% | 0.00192 | 11.8s | 4.58 | 83% | 8% |
  | openai/gpt-4o-mini | 100% | 0.00042 | 3.6s | 3.75 | 83% | 17% |
  | openai/gpt-4.1-mini | 100% | 0.00102 | 2.7s | 4.00 | 67% | 17% |
  | z-ai/glm-5.3-flash | 100% | 0.00145 | 8.4s | 4.17 | 67% | 17% |
  | qwen/qwen3.6–3.8-flash | 100% | 0.0005–0.0044 | 16–38s | 3.8–4.0 | 58–67% | 17% |
  | qwen/qwen3.5-flash-02-23 | **0%** | — | 2.6s | 1.0 | 0% | 100% |

  The incumbent wins on what matters for re-work: highest key-field accuracy
  (92%), zero fabrication, and lowest cost-per-correct-record. Newer qwen
  flash generations regressed. `google/gemini-3.1-flash-lite` is the
  documented fast alternative (~10x lower latency at 2x cost, slightly lower
  accuracy) via `OPENROUTER_EXTRACT_MODEL` for burst situations.

Corpus result on the current crawl (72 sources, `--reference-date 2026-09-19`):
**17,838 chunks → 14,776 kept / 3,062 filtered**; **3,948 feed items → 1,244 kept
/ 2,704 filtered** (WDET's archive). Kept-chunk breakdown: ~8.9k undated,
~2k future, ~2k publish/ambiguous within 6 months, ~0.7k known events within
2 weeks; 245 4xx and 54 duplicate pages dropped.

Built since the first pass (see [`DATAFLOW.md`](DATAFLOW.md) for the
end-to-end map):

- Real Jev clients (`OpenRouterJevClient`, `TypeSafeJevClient`) + calibrated
  thresholds tuned on the round-2 gold set.
- Dedupe (stage 5), the deterministic router (6), LLM extraction (7),
  validation/publish (8), and the idempotent store (9).
- The incremental pipeline (`extract/pipeline.py`) and scheduled publisher
  (`run_collect.sh`).


## Gold set

Hand-labeled samples live in `gold/`. That directory is **local-only and
gitignored** (`.gitignore` → `gold/`): labels are hand-made and not
reproducible, and no round is committed, so the `gold/round*/…` paths referenced
below (and elsewhere in this doc) exist only on the machine that ran them. Each
round records its seed and corpus fingerprint so rounds are comparable.

```sh
python3 -m extract.sample --n 80 --seed 5 --per-source-cap 2 \
    --out gold/round1/candidates.jsonl
# label into gold/round1/labels.jsonl, then:
python3 -m extract.score_gold gold/round1/labels.jsonl
```

**Round 1** (80 chunks, 51-source corpus) was run locally; see
`gold/round1/FINDINGS.md` (if present). Headline results: only 21% of chunks are
actionable now, mechanical `hint` precision is ~0.4–0.5, feeds are the most
mechanical source, documents/homepages are mostly noise, and
staleness/locality dominate.

**Round 2** was run locally after full collection (~75 sources) with a fresh
seed and a larger sample; compare via `score_gold`. Its reports
(`CHARACTERIZATION-REPORT.md`, `MODEL-BENCH.md`, `SCORING.md`) are cited
throughout this doc but are not committed to the repo.

## Suggested order of work

1. ~~**Funnel (this pass)** — stages 0–3, runnable, inspectable output.~~ **done**
2. ~~**Gold set** — hand-label ~100 chunks; use it to tune Jev thresholds and the
   router. Calibrated confidence only helps if you measure it.~~ **done (rounds 1–2)**
3. ~~**Jev gate** — wire the real API once off the waitlist.~~ **done**
4. ~~**Dedupe + router + extraction** — add the OpenRouter tier.~~ **done**
5. ~~**Validation + store** — publish gate and idempotent upsert.~~ **done**

Next: see [`FUNNEL-RECOMMENDATIONS.md`](FUNNEL-RECOMMENDATIONS.md) for the
funnel backlog and [`CRAWLER-RECOMMENDATIONS.md`](CRAWLER-RECOMMENDATIONS.md)
for collector work.