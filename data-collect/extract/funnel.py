#!/usr/bin/env python3
"""Mechanical extraction funnel (stages 0-3) for the Daily Brief corpus.

Reads the crawler's output (``crawl/<slug>/{content.md,meta.json}``, treated as
immutable input) and writes a parallel, processed tree::

    processed/
        <slug>/
            chunks.jsonl     in-window stage-3 chunks (input to the Jev gate)
            chunks_dropped.jsonl   date-filtered-out chunks (kept for audit)
            feed_items.jsonl       in-window structured RSS/Atom/ICS records
            feed_items_dropped.jsonl  date-filtered-out feed items
            funnel.json      per-source stats + input fingerprint
            pages.jsonl      cleaned page text (only with --emit-pages)
        summary.json         aggregate run stats
        .state.json          per-source fingerprints for --changed-only

What it does, per source:

    0. Change detection  - fingerprint the source by its pages' sha256 values;
                           skip unchanged sources with --changed-only.
    1. Segment + class   - split content.md on <!-- page N --> / <!-- asset N -->
                           markers, join to meta.json by (kind, index), drop
                           failed/4xx pages and duplicate sha256s, classify each
                           URL into a small url_class vocab.
    2. De-boilerplate    - drop lines that recur across most of a source's
                           pages (nav/footer chrome) and long pure link-lists.
    3. Chunk + feeds     - RSS/Atom/ICS blocks are parsed structurally into
                           feed_items.jsonl (RSS -> news, ICS -> event) and are
                           not chunked by default; everything else becomes
                           heading-aware chunks with provenance, mechanical
                           candidate hints, and normalized Eastern-assumed dates
                           with recency signals.
    4. Recency filter    - keep all future dates and undated records; drop known
                           event dates older than --max-age-days (14) and
                           publish/ambiguous dates older than
                           --publish-max-age-days (183). Bias: when in doubt,
                           keep more. Disable with --no-filter.

The output is deliberately model-free: stage 4 (the Jev candidate gate) consumes
``chunks.jsonl``. See ``EXTRACTION.md`` for the full pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from . import dates, dedupe, feeds, locality, urls

UTC = timezone.utc
SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_FILE = SCRIPT_DIR / "source.json"
DEFAULT_CRAWL_DIR = SCRIPT_DIR / "crawl"
DEFAULT_OUT_DIR = SCRIPT_DIR / "processed"
FUNNEL_VERSION = "0.1.0"

MARKER_RE = re.compile(r"<!--\s*(page|asset)\s+(\d+)\s*-->")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
IMAGE_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")

# URL path rules -> url_class. Order matters (first match wins).
_URL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/feed(?:/|$)|/rss(?:/|$)|/atom(?:/|$)|\.(?:rss|atom|xml)(?:$|\?)"), "feed"),
    (re.compile(r"\.pdf(?:$|\?)|/wp-content/|/downloads?(?:/|$)|/document(?:center|s)?(?:/|$)|/media/"), "document"),
    (re.compile(r"/events?(?:/|$)|/calendar|/shows?(?:/|$)|/performances?(?:/|$)|/exhibits?(?:/|$)|/programs?(?:/|$)"), "event"),
    (re.compile(r"/agendas?(?:/|$)|/minutes(?:/|$)|/meetings?(?:/|$)|civicclerk|/board-of|/board(?:/|$)"), "agenda"),
    (re.compile(r"/news(?:/|$)|/press(?:-|/|$)|/announcements?(?:/|$)|/article|/stories(?:/|$)|/blog|/post(?:s|/|$)|/alerts?"), "news"),
    (re.compile(r"/about|/contact|/privacy|/terms|/staff|/directory|/employment|/jobs|/careers|/faq|/hours|/locations?(?:/|$)"), "utility"),
]

_EVENT_KEYWORDS = [
    "event", "events", "calendar", "upcoming", "join us", "register", "registration",
    "tickets", "concert", "performance", "exhibit", "exhibition", "festival", "workshop",
    "storytime", "story time", "open mic", "screening", "audition", "reception", "tour",
    "fundraiser", "parade", "market", "nature walk", "hike", "lecture", "class", "drop-in",
]
_NEWS_KEYWORDS = [
    "announce", "announced", "announcement", "press release", "for immediate release",
    "update", "approved", "council", "trustee", "budget", "proposal", "public notice",
    "ordinance", "road closure", "construction", "hiring", "grant", "award", "recognized",
    "investigation", "police", "fire department", "school", "students", "district",
]
_NOTICE_KEYWORDS = ["public notice", "legal notice", "notice of", "agenda", "minutes"]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def slugify(name: str) -> str:
    """Mirror crawl_sources.slugify so source names map to crawl/ dirs."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:80].strip("-")
    return slug or hashlib.sha1(name.encode()).hexdigest()[:12]


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Good enough for chunk sizing."""
    return max(1, len(text) // 4)


def _sha1(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Stage 1: segment + URL classification
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    kind: str                 # "page" | "asset"
    index: int                # index within meta.json's pages list
    body: str                 # raw body text from content.md
    body_offset: int          # offset of body within content.md
    url: str = ""
    final_url: str = ""
    title: str = ""
    status: int | None = None
    depth: int | None = None
    sha256: str = ""
    url_class: str = "other"
    image: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status is None or 200 <= self.status < 400


def classify_url(url: str) -> str:
    path = re.sub(r"^https?://[^/]+", "", url).split("?")[0].split("#")[0]
    low = path.lower()
    for pattern, label in _URL_RULES:
        if pattern.search(low):
            return label
    if low in ("", "/"):
        return "home"
    return "other"


def split_content(content: str) -> Iterator[tuple[str, int, int, str]]:
    """Yield (kind, index, body_offset, body) for each marker in content.md."""
    matches = list(MARKER_RE.finditer(content))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        yield m.group(1), int(m.group(2)), start, content[start:end]


_GENERATOR_LINE_RE = re.compile(r"^(?:URL:|Status:|Error:|---+)\s*")


def strip_generator_header(body: str) -> str:
    """Drop the synthetic ``# Title`` / ``URL:`` / ``Status:`` header that
    ``crawl_sources.py`` writes at the top of every page block, plus separators.
    The same information is preserved in ``meta.json`` and in chunk fields.
    """
    lines = body.splitlines()
    i = 0
    n = len(lines)
    while i < n and not lines[i].strip():
        i += 1
    if i < n and lines[i].lstrip().startswith("# "):
        i += 1
    while i < n and (_GENERATOR_LINE_RE.match(lines[i].strip()) or not lines[i].strip()):
        i += 1
    out = lines[i:]
    while out and (_GENERATOR_LINE_RE.match(out[-1].strip()) or not out[-1].strip()):
        out.pop()
    return "\n".join(out)


def load_meta(meta_path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    if not meta_path.exists():
        return {}
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for page in meta.get("crawl", {}).get("pages", []):
        kind = page.get("kind", "page")
        index = page.get("index")
        if index is not None:
            out[(kind, int(index))] = page
    return out


def segment_source(
    slug_dir: Path,
    *,
    drop_4xx: bool = True,
    include_assets: bool = True,
) -> tuple[list[Segment], dict[str, int]]:
    """Stage 1. Returns (kept_segments, drop_counts)."""
    content_path = slug_dir / "content.md"
    meta = load_meta(slug_dir / "meta.json")
    if not content_path.exists():
        return [], {"missing_content": 1}

    content = content_path.read_text(encoding="utf-8", errors="replace")
    kept: list[Segment] = []
    seen_sha: dict[str, Segment] = {}
    counts = {"raw": 0, "dropped_4xx": 0, "dropped_dupe": 0, "dropped_asset": 0}

    for kind, index, offset, body in split_content(content):
        counts["raw"] += 1
        if kind == "asset" and not include_assets:
            counts["dropped_asset"] += 1
            continue
        info = meta.get((kind, index), {})
        status = info.get("status_code")
        seg = Segment(
            kind=kind,
            index=index,
            body=strip_generator_header(body),
            body_offset=offset,
            url=info.get("url", ""),
            final_url=info.get("final_url", "") or info.get("url", ""),
            title=info.get("title", ""),
            status=status,
            depth=info.get("depth"),
            sha256=info.get("sha256", ""),
            image=(info.get("image") or "").strip(),
            images=[u for u in (info.get("images") or []) if u][:8],
            url_class=classify_url(info.get("url", "")),
        )
        if drop_4xx and not seg.ok:
            counts["dropped_4xx"] += 1
            continue
        if seg.sha256:
            prev = seen_sha.get(seg.sha256)
            if prev is not None:
                counts["dropped_dupe"] += 1
                continue
            seen_sha[seg.sha256] = seg
        kept.append(seg)

    return kept, counts


# --------------------------------------------------------------------------- #
# Stage 2: de-boilerplate
# --------------------------------------------------------------------------- #

def _line_key(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def _looks_like_link_list(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 200:
        return False
    links = LINK_RE.findall(stripped)
    if len(links) < 5:
        return False
    return sum(len(l) for l in links) >= 0.5 * len(stripped)


def _boilerplate_keys(segments: list[Segment]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for seg in segments:
        keys = set()
        for line in seg.body.splitlines():
            key = _line_key(line)
            if len(key) >= 20 or _looks_like_link_list(line):
                keys.add(key)
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    return counts


def clean_body(body: str, counts: dict[str, int], threshold: float) -> str:
    """Drop recurring/boilerplate lines from one segment body."""
    kept: list[str] = []
    blank_run = 0
    for line in body.splitlines():
        key = _line_key(line)
        drop = False
        if IMAGE_ONLY_RE.match(line):
            drop = True
        elif key and counts.get(key, 0) > threshold:
            drop = True
        elif _looks_like_link_list(line):
            drop = True
        if drop:
            continue
        if not key:
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        kept.append(line)
    return "\n".join(kept).strip("\n")


def deboilerplate(
    segments: list[Segment],
    *,
    threshold: float = 0.5,
    min_segments: int = 4,
) -> list[Segment]:
    """Stage 2. Mutates segment bodies in place to cleaned text."""
    if len(segments) < min_segments:
        for seg in segments:
            seg.body = clean_body(seg.body, {}, 1.0)
        return segments
    counts = _boilerplate_keys(segments)
    cutoff = threshold * len(segments)
    for seg in segments:
        seg.body = clean_body(seg.body, counts, cutoff)
    return segments


# --------------------------------------------------------------------------- #
# Candidate hints (features for the Jev gate; no model calls here)
# --------------------------------------------------------------------------- #

def _keyword_hits(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    return [kw for kw in keywords if kw in low]


def candidate_signals(
    text: str,
    url_class: str,
    *,
    ref: Optional[datetime] = None,
    context_year: Optional[int] = None,
    normalized: Optional[list] = None,
    url: str = "",
    source_tier: Optional[int] = None,
    source_location: str = "",
) -> dict[str, Any]:
    if normalized is None:
        normalized = dates.extract_dates(text, ref=ref, context_year=context_year)
    date_summary = dates.summarize(normalized, ref=ref)
    event_hits = _keyword_hits(text, _EVENT_KEYWORDS)
    news_hits = _keyword_hits(text, _NEWS_KEYWORDS)
    notice_hits = _keyword_hits(text, _NOTICE_KEYWORDS)
    return {
        "url_class": url_class,
        "has_date": bool(normalized),
        "dates": [d.source_text for d in normalized],
        # Normalized to absolute ISO-8601, Eastern-assumed when no offset.
        "normalized_dates": [d.iso for d in normalized],
        "date_spans": [
            {
                "iso": d.iso,
                "date": d.date,
                "has_time": d.has_time,
                "year_assumed": d.year_assumed,
                "tz_assumed": d.tz_assumed,
            }
            for d in normalized
        ],
        "has_time": date_summary["has_time"],
        "date_recency": date_summary["date_recency"],
        "has_future_date": date_summary["has_future_date"],
        "nearest_days_from_ref": date_summary["nearest_days_from_ref"],
        "year_assumed_count": date_summary["year_assumed_count"],
        "event_score": len(event_hits),
        "news_score": len(news_hits),
        "notice_score": len(notice_hits),
        "event_terms": event_hits[:8],
        "news_terms": news_hits[:8],
        "notice_terms": notice_hits[:8],
        "locality": locality.detect(
            text, url, source_tier=source_tier, source_location=source_location
        ),
    }


def candidate_hint(signals: dict[str, Any]) -> str:
    cls = signals["url_class"]
    if cls == "agenda":
        return "agenda"
    if cls == "event":
        return "event"
    if cls == "news":
        return "news"
    if signals["notice_score"] and signals["has_date"]:
        return "notice"
    if signals["event_score"] and signals["has_date"]:
        return "event"
    if signals["event_score"] > signals["news_score"] and signals["event_score"] >= 2:
        return "event"
    if signals["news_score"] >= 2:
        return "news"
    return "unknown"


# --------------------------------------------------------------------------- #
# Stage 3: heading-aware chunking
# --------------------------------------------------------------------------- #

def _iter_lines_with_offsets(text: str) -> Iterator[tuple[int, str]]:
    pos = 0
    for line in text.splitlines(keepends=True):
        yield pos, line
        pos += len(line)


@dataclass
class Chunk:
    text: str
    heading: str
    char_start: int
    char_end: int


def chunk_text(
    text: str,
    *,
    target_tokens: int = 900,
    max_tokens: int = 1400,
    min_tokens: int = 80,
    overlap: int = 0,
) -> list[Chunk]:
    """Heading-aware chunker. Offsets are relative to the cleaned page text."""
    chunks: list[Chunk] = []
    cur: list[tuple[int, str]] = []
    heading = ""

    def cur_text() -> str:
        return "".join(line for _, line in cur)

    def flush() -> list[tuple[int, str]]:
        nonlocal cur
        body = cur_text().strip("\n")
        if body.strip():
            chunks.append(
                Chunk(
                    text=body,
                    heading=heading,
                    char_start=cur[0][0],
                    char_end=cur[-1][0] + len(cur[-1][1]),
                )
            )
        if overlap and cur:
            # Carry the trailing ~overlap tokens of source lines into the next chunk.
            tail: list[tuple[int, str]] = []
            budget = overlap
            for pos, line in reversed(cur):
                t = estimate_tokens(line)
                if tail and budget - t < 0:
                    break
                tail.insert(0, (pos, line))
                budget -= t
            cur = tail
        else:
            cur = []
        return cur

    for pos, line in _iter_lines_with_offsets(text):
        m = HEADING_RE.match(line.rstrip("\n"))
        if m:
            if cur and estimate_tokens(cur_text()) >= min_tokens:
                flush()
            heading = m.group(2).strip()
        cur.append((pos, line))
        if estimate_tokens(cur_text()) >= max_tokens:
            flush()

    if cur:
        flush()

    # Merge a tiny trailing chunk into its predecessor.
    if len(chunks) >= 2 and estimate_tokens(chunks[-1].text) < min_tokens:
        last = chunks.pop()
        prev = chunks[-1]
        prev.text = f"{prev.text}\n\n{last.text}".strip()
        prev.char_end = last.char_end
    return chunks


# --------------------------------------------------------------------------- #
# Per-source pipeline
# --------------------------------------------------------------------------- #

def source_fingerprint(segments: list[Segment]) -> str:
    parts = sorted(
        seg.sha256 or f"{seg.kind}:{seg.index}:{seg.url}" for seg in segments
    )
    return _sha1(FUNNEL_VERSION, *parts)[:16]


def process_source(
    slug: str,
    source: dict[str, Any],
    crawl_dir: Path,
    out_dir: Path,
    opts: argparse.Namespace,
) -> dict[str, Any]:
    slug_dir = crawl_dir / slug
    segments, drop_counts = segment_source(
        slug_dir,
        drop_4xx=opts.drop_4xx,
        include_assets=opts.include_assets,
    )
    if not segments:
        return {
            "slug": slug,
            "name": source.get("name", slug),
            "skipped": "no_segments",
            **drop_counts,
        }

    fingerprint = source_fingerprint(segments)
    if opts.changed_only and not opts.force:
        state = _load_state(out_dir)
        if state.get(slug, {}).get("fingerprint") == fingerprint:
            return {
                "slug": slug,
                "name": source.get("name", slug),
                "skipped": "unchanged",
                "fingerprint": fingerprint,
                **drop_counts,
            }

    deboilerplate(
        segments,
        threshold=opts.boilerplate_threshold,
        min_segments=opts.min_pages_for_boilerplate,
    )

    source_slug = slug
    source_name = source.get("name", slug)
    source_type = source.get("type", "")
    locality_index = source.get("locality_index")
    location = source.get("location", "")

    dest = out_dir / slug
    dry = bool(getattr(opts, "stats", False))
    ref = getattr(opts, "reference_dt", None) or datetime.now(UTC)
    if not dry:
        dest.mkdir(parents=True, exist_ok=True)

    chunk_records: list[dict[str, Any]] = []
    chunk_dropped: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    raw_feed_items: list[dict[str, Any]] = []
    hint_counts: dict[str, int] = {}
    class_counts: dict[str, int] = {}
    total_tokens = 0
    feed_blocks = 0
    do_filter = getattr(opts, "filter_dates", True)
    drop_oaa = getattr(opts, "drop_out_of_area", False)
    known_days = getattr(opts, "max_age_days", 14)
    publish_days = getattr(opts, "publish_max_age_days", 183)

    for seg in segments:
        class_counts[seg.url_class] = class_counts.get(seg.url_class, 0) + 1
        cleaned = seg.body
        if opts.emit_pages:
            page_records.append({
                "source_slug": source_slug,
                "kind": seg.kind,
                "index": seg.index,
                "url": seg.url,
                "title": seg.title,
                "status": seg.status,
                "depth": seg.depth,
                "sha256": seg.sha256,
                "url_class": seg.url_class,
                "chars": len(cleaned),
                "tokens": estimate_tokens(cleaned),
                "text": cleaned,
            })

        # Stage 3a: feeds are parsed structurally, not chunked.
        kind, items = feeds.parse_feed_markdown(cleaned, seg.url)
        if kind and not opts.keep_feed_chunks:
            feed_blocks += 1
            for it in items:
                raw_feed_items.append({**it, "page_index": seg.index,
                                       "page_sha256": seg.sha256})
            continue

        context_year = dates.infer_context_year(seg.url, seg.title)
        for chunk in chunk_text(
            cleaned,
            target_tokens=opts.target_tokens,
            max_tokens=opts.max_tokens,
            min_tokens=opts.min_tokens,
            overlap=opts.overlap,
        ):
            normalized = dates.extract_dates(
                chunk.text, ref=ref, context_year=context_year
            )
            signals = candidate_signals(
                chunk.text,
                seg.url_class,
                ref=ref,
                context_year=context_year,
                normalized=normalized,
                url=seg.url,
                source_tier=locality_index,
                source_location=location,
            )
            hint = candidate_hint(signals)
            # A date in event-like content is a *known* date; otherwise treat the
            # date as a publish/mention date (lenient window).
            role = "event" if hint == "event" else "publish"
            entries = [(d.dt, d.year_assumed, role) for d in normalized]
            if do_filter:
                keep, reason = dates.within_window(
                    entries, ref, known_days=known_days, publish_days=publish_days
                )
            else:
                keep, reason = True, "unfiltered"
            if drop_oaa and signals["locality"]["out_of_area"]:
                keep, reason = False, "out_of_area"
            hint_counts[hint] = hint_counts.get(hint, 0) + 1
            tokens = estimate_tokens(chunk.text)
            if keep:
                total_tokens += tokens
            record = {
                "chunk_id": _sha1(
                    FUNNEL_VERSION, source_slug, seg.sha256 or seg.url,
                    str(chunk.char_start), str(chunk.char_end), chunk.text,
                )[:16],
                "source_slug": source_slug,
                "source_name": source_name,
                "source_type": source_type,
                "locality_index": locality_index,
                "location": location,
                "page_kind": seg.kind,
                "page_index": seg.index,
                "url": seg.url,
                "final_url": seg.final_url,
                "page_title": seg.title,
                "page_sha256": seg.sha256,
                "page_image": seg.image,
                "page_images": seg.images,
                "url_class": seg.url_class,
                "heading": chunk.heading,
                "page_char_offset": seg.body_offset,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "tokens": tokens,
                "content_hash": hashlib.sha256(
                    re.sub(r"\s+", " ", chunk.text).encode("utf-8", "replace")
                ).hexdigest(),
                "candidate_hint": hint,
                "signals": signals,
                "in_window": keep,
                "filter_reason": reason,
                "text": chunk.text,
            }
            (chunk_records if keep else chunk_dropped).append(record)

    if not dry:
        _write_jsonl(dest / "chunks.jsonl", chunk_records)
        _write_jsonl(dest / "chunks_dropped.jsonl", chunk_dropped)
        if opts.emit_pages:
            _write_jsonl(dest / "pages.jsonl", page_records)

    # Stage 3b: enrich + dedupe feed items. RSS -> news, ICS -> event.
    # F2: index crawled pages by canonical URL so feed items can join to them.
    page_by_url: dict[str, Segment] = {}
    for seg in segments:
        for candidate in (seg.final_url, seg.url):
            if candidate:
                page_by_url.setdefault(urls.canonical_url(candidate), seg)
    feed_records: list[dict[str, Any]] = []
    feed_dropped: list[dict[str, Any]] = []
    feed_joined = 0
    for it in feeds.dedupe_items(raw_feed_items):
        text = "\n".join(
            str(it.get(k) or "")
            for k in ("title", "summary", "venue")
        )
        signals = candidate_signals(
            text,
            "feed",
            ref=ref,
            context_year=dates.infer_context_year(
                it.get("feed_url") or "", it.get("title") or ""
            ),
            url=it.get("url") or it.get("feed_url") or "",
            source_tier=locality_index,
            source_location=location,
        )
        is_ics = it.get("feed_kind") == "ics"
        if is_ics:
            hint = "event"
        else:
            hint = candidate_hint(signals)
            if hint == "unknown":
                hint = "news"
        hint_counts[hint] = hint_counts.get(hint, 0) + 1
        # ICS DTSTART is a known event date; an RSS pubDate is a publish date.
        role = "event" if is_ics else "publish"
        entries: list[tuple[datetime, bool, str]] = []
        stamp = it.get("start") if is_ics else it.get("publishedAt")
        if stamp:
            try:
                dt = datetime.fromisoformat(stamp)
                it["age_days"] = dates.age_days(dt, ref)
                it["recency"] = dates.recency(dt, ref)
                entries = [(dt, False, role)]
                if is_ics:
                    # DTSTART is authoritative: the item's title/summary often
                    # carries no date for the scanners to find. Promote it into
                    # the signals used for prioritisation, the Jev hints, and
                    # the extraction prompt's known_dates.
                    known = [it["start"]]
                    if it.get("end"):
                        known.append(it["end"])
                    signals["has_date"] = True
                    signals["has_time"] = "T00:00:00" not in it["start"]
                    signals["dates"] = [stamp]
                    signals["normalized_dates"] = known
                    signals["date_recency"] = it["recency"]
                    signals["has_future_date"] = it["recency"] == "future"
                    signals["nearest_days_from_ref"] = it["age_days"]
            except ValueError:
                pass
        if do_filter:
            keep, reason = dates.within_window(
                entries, ref, known_days=known_days, publish_days=publish_days
            )
        else:
            keep, reason = True, "unfiltered"
        if drop_oaa and signals["locality"]["out_of_area"]:
            keep, reason = False, "out_of_area"
        # F2: join to an already-crawled page when possible (no fetch needed).
        matched = page_by_url.get(urls.canonical_url(it.get("url")))
        if matched is not None:
            feed_joined += 1
        record = {
            **it,
            "source_slug": source_slug,
            "source_name": source_name,
            "source_type": source_type,
            "locality_index": locality_index,
            "location": location,
            "target": "event" if is_ics else "news",
            "candidate_hint": hint,
            "signals": signals,
            "in_window": keep,
            "filter_reason": reason,
            "article_crawled": matched is not None,
            "article_page_index": matched.index if matched else None,
            "article_page_sha256": matched.sha256 if matched else None,
        }
        (feed_records if keep else feed_dropped).append(record)

    if not dry:
        _write_jsonl(dest / "feed_items.jsonl", feed_records)
        _write_jsonl(dest / "feed_items_dropped.jsonl", feed_dropped)

    stats = {
        "slug": slug,
        "name": source_name,
        "type": source_type,
        "fingerprint": fingerprint,
        "funnel_version": FUNNEL_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reference_date": ref.astimezone(dates.EASTERN).date().isoformat(),
        "segments_kept": len(segments),
        "chunks": len(chunk_records),
        "chunks_filtered": len(chunk_dropped),
        "feed_blocks": feed_blocks,
        "feed_items": len(feed_records),
        "feed_items_filtered": len(feed_dropped),
        "feed_items_joined": feed_joined,
        "chunks_out_of_area": sum(
            1 for r in chunk_records if r["signals"]["locality"]["out_of_area"]
        ),
        "feed_items_out_of_area": sum(
            1 for r in feed_records if r["signals"]["locality"]["out_of_area"]
        ),
        "tokens": total_tokens,
        "url_classes": class_counts,
        "candidate_hints": hint_counts,
        **drop_counts,
    }
    if not dry:
        (dest / "funnel.json").write_text(
            json.dumps(stats, indent=2) + "\n", encoding="utf-8"
        )

    if opts.changed_only and not opts.force and not dry:
        state = _load_state(out_dir)
        state[slug] = {
            "fingerprint": fingerprint,
            "chunks": len(chunk_records),
            "updated_at": stats["generated_at"],
        }
        _save_state(out_dir, state)

    return stats


# --------------------------------------------------------------------------- #
# I/O + guards
# --------------------------------------------------------------------------- #

def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _load_state(out_dir: Path) -> dict[str, Any]:
    path = out_dir / ".state.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(out_dir: Path, state: dict[str, Any]) -> None:
    (out_dir / ".state.json").write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def guard_output(out_dir: Path, crawl_dir: Path) -> None:
    """Refuse to write anywhere inside the read-only crawl tree."""
    out = out_dir.resolve()
    crawl = crawl_dir.resolve()
    if out == crawl or crawl in out.parents:
        raise SystemExit(
            f"refusing to write inside the crawl input tree: {out}\n"
            f"choose an output directory parallel to {crawl} (e.g. processed/)"
        )


def load_sources(source_file: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(source_file.read_text(encoding="utf-8"))
    sources = data.get("sources", data if isinstance(data, list) else [])
    out: dict[str, dict[str, Any]] = {}
    for src in sources:
        name = src.get("name")
        if name:
            out[slugify(name)] = src
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m extract",
        description="Mechanical extraction funnel (stages 0-3) over the crawl corpus.",
    )
    p.add_argument("--source-file", type=Path, default=DEFAULT_SOURCE_FILE)
    p.add_argument("--crawl-dir", type=Path, default=DEFAULT_CRAWL_DIR)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR,
                   help="output tree, parallel to crawl/ (default: processed/)")
    p.add_argument("--source", action="append", default=None,
                   help="only process this source slug (repeatable)")
    p.add_argument("--limit", type=int, default=None,
                   help="process at most N sources")
    p.add_argument("--include-assets", dest="include_assets",
                   action="store_true", default=True)
    p.add_argument("--no-assets", dest="include_assets", action="store_false")
    p.add_argument("--keep-4xx", dest="drop_4xx", action="store_false", default=True)
    p.add_argument("--changed-only", action="store_true",
                   help="skip sources whose input fingerprint is unchanged")
    p.add_argument("--force", action="store_true",
                   help="with --changed-only, reprocess anyway")
    p.add_argument("--emit-pages", action="store_true",
                   help="also write cleaned page text to pages.jsonl")
    p.add_argument("--keep-feed-chunks", action="store_true",
                   help="also chunk feed blocks (default: parse feeds structurally only)")
    # Stage 2
    p.add_argument("--boilerplate-threshold", type=float, default=0.5)
    p.add_argument("--min-pages-for-boilerplate", type=int, default=4)
    # Stage 3
    p.add_argument("--target-tokens", type=int, default=900)
    p.add_argument("--max-tokens", type=int, default=1400)
    p.add_argument("--min-tokens", type=int, default=80)
    p.add_argument("--overlap", type=int, default=0)
    p.add_argument("--reference-date", default=None,
                   help="YYYY-MM-DD reference for date recency (default: today Eastern)")
    # Recency filter: known event dates vs publish/ambiguous dates.
    p.add_argument("--max-age-days", type=int, default=14,
                   help="drop known event dates older than N days (default 14)")
    p.add_argument("--publish-max-age-days", type=int, default=183,
                   help="keep publish/ambiguous dates up to N days old (default 183)")
    p.add_argument("--no-filter", dest="filter_dates", action="store_false",
                   default=True,
                   help="keep all dated records (disable the 2-week/6-month filter)")
    p.add_argument("--drop-out-of-area", action="store_true", default=False,
                   help="also drop items detected as out of area (default: keep)")
    # Stage 5
    p.add_argument("--no-dedupe", action="store_true", default=False,
                   help="skip cross-source dedupe post-pass")
    p.add_argument("--dedupe-threshold", type=int, default=3,
                   help="SimHash hamming distance for near-dupes (default 3)")
    p.add_argument("--stats", action="store_true",
                   help="print aggregate stats only, do not write files")
    p.add_argument("--list-sources", action="store_true",
                   help="list crawl source slugs and exit")
    return p


def run(argv: Optional[list[str]] = None) -> int:
    opts = build_parser().parse_args(argv)
    guard_output(opts.out, opts.crawl_dir)
    try:
        opts.reference_dt = dates.parse_reference(opts.reference_date)
    except ValueError:
        _log(f"invalid --reference-date: {opts.reference_date!r} (want YYYY-MM-DD)")
        return 2

    sources = load_sources(opts.source_file)
    slugs = sorted(
        d.name for d in opts.crawl_dir.iterdir() if d.is_dir()
    ) if opts.crawl_dir.exists() else []
    if opts.source:
        wanted = {slugify(s) if s not in sources else s for s in opts.source}
        slugs = [s for s in slugs if s in wanted]
    if opts.limit:
        slugs = slugs[: opts.limit]
    if not slugs:
        _log("no crawl sources found")
        return 1

    if opts.list_sources:
        for slug in slugs:
            print(slug)
        return 0

    opts.out.mkdir(parents=True, exist_ok=True)
    reference_iso = opts.reference_dt.astimezone(dates.EASTERN).date().isoformat()
    _log(f"reference date: {reference_iso} (dates without tz assume America/Detroit)")
    summary: list[dict[str, Any]] = []
    for i, slug in enumerate(slugs, 1):
        source = sources.get(slug, {"name": slug})
        stats = process_source(slug, source, opts.crawl_dir, opts.out, opts)
        summary.append(stats)
        skipped = stats.get("skipped")
        status = f"skipped ({skipped})" if skipped else (
            f"{stats.get('chunks', 0)} chunks / {stats.get('tokens', 0)} tok"
        )
        _log(f"[{i}/{len(slugs)}] {slug}: {status}")

    if not opts.stats and not opts.no_dedupe:
        _log("dedupe: clustering across sources...")
        dedupe_report = dedupe.apply(opts.out, threshold=opts.dedupe_threshold)
        for stream, info in dedupe_report["streams"].items():
            _log(
                f"dedupe/{stream}: {info['duplicates']}/{info['records']} "
                f"duplicates in {info['groups']} groups"
            )
    else:
        dedupe_report = None

    if not opts.stats:
        stream_stats = (dedupe_report or {}).get("streams", {})
        totals = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "funnel_version": FUNNEL_VERSION,
            "reference_date": reference_iso,
            "sources": len(summary),
            "skipped": sum(1 for s in summary if s.get("skipped")),
            "chunks": sum(s.get("chunks", 0) for s in summary),
            "chunks_filtered": sum(s.get("chunks_filtered", 0) for s in summary),
            "chunks_out_of_area": sum(s.get("chunks_out_of_area", 0) for s in summary),
            "chunks_duplicates": stream_stats.get("chunks", {}).get("duplicates", 0),
            "feed_items": sum(s.get("feed_items", 0) for s in summary),
            "feed_items_filtered": sum(s.get("feed_items_filtered", 0) for s in summary),
            "feed_items_out_of_area": sum(s.get("feed_items_out_of_area", 0) for s in summary),
            "feed_items_joined": sum(s.get("feed_items_joined", 0) for s in summary),
            "feed_items_duplicates": stream_stats.get("feed_items", {}).get("duplicates", 0),
            "tokens": sum(s.get("tokens", 0) for s in summary),
            "dropped_4xx": sum(s.get("dropped_4xx", 0) for s in summary),
            "dropped_dupe": sum(s.get("dropped_dupe", 0) for s in summary),
        }
        (opts.out / "summary.json").write_text(
            json.dumps({"totals": totals, "sources": summary}, indent=2) + "\n",
            encoding="utf-8",
        )
        _log(
            f"total: {totals['chunks']} chunks / {totals['tokens']} tokens across "
            f"{totals['sources']} sources -> {opts.out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())