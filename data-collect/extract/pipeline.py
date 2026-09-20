#!/usr/bin/env python3
"""Full extraction pipeline: processed chunks -> triage -> route -> records.

    .venv/bin/python -m extract.pipeline --max-items 400 --extract-limit 150

Reads canonical, in-window chunks and feed items from ``processed/``, prioritises
the ones most likely to be brief-worthy, triages with Jev (concurrent), routes,
extracts with the light model (concurrent), and writes schema-shaped records.

Everything is bounded: ``--max-items`` caps Jev calls, ``--extract-limit`` caps
generation calls. Record ids/addedAt/localityIndex are assigned here, not by the
model.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import dates, enrich, jev, llm, prompts, router, scoring

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROCESSED = SCRIPT_DIR / "processed"
DEFAULT_OUT = SCRIPT_DIR / "processed" / "extracted_records.jsonl"
_CONTENT_HINTS = {"event", "news", "notice", "agenda"}
_NULLISH = {"", "null", "none", "n/a", "unknown", "not specified"}


# --------------------------------------------------------------------------- #
# Loading + prioritisation
# --------------------------------------------------------------------------- #

def load_candidates(processed: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(processed.glob("*/chunks.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if not rec.get("is_canonical", True) or not rec.get("in_window", True):
                continue
            sig = rec.get("signals", {})
            if sig.get("locality", {}).get("out_of_area"):
                continue
            if rec.get("candidate_hint") not in _CONTENT_HINTS:
                continue
            rec["_kind"] = "chunk"
            items.append(rec)
    for path in sorted(processed.glob("*/feed_items.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if not rec.get("is_canonical", True) or not rec.get("in_window", True):
                continue
            if rec.get("signals", {}).get("locality", {}).get("out_of_area"):
                continue
            rec["_kind"] = "feed"
            items.append(rec)
    return items


def priority(rec: dict[str, Any], ref: datetime) -> tuple:
    sig = rec.get("signals", {})
    recency = sig.get("date_recency", "none")
    days = sig.get("nearest_days_from_ref")
    near = abs(days) if isinstance(days, (int, float)) else 999
    return (
        0 if rec["_kind"] == "feed" else 1,
        0 if recency in ("today", "future") else 1,
        0 if rec.get("article_crawled") else 1,
        near,
        str(rec.get("chunk_id") or rec.get("item_id") or ""),
    )


def select(items: list[dict[str, Any]], limit: int, ref: datetime) -> list[dict[str, Any]]:
    return sorted(items, key=lambda r: priority(r, ref))[:limit]


# --------------------------------------------------------------------------- #
# Validation / normalisation
# --------------------------------------------------------------------------- #

def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return None if value.strip().lower() in _NULLISH else value.strip()
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v)]
    return value


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-")


def finalize(record: dict[str, Any], origin: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Clean a model record and attach provenance/ids; drop useless records."""
    for key in ("title", "summary", "start", "end", "venue", "city", "category",
                "price", "registration", "url", "author", "publication", "publishedAt",
                "imageUrl", "imageAlt"):
        if key in record:
            record[key] = _clean(record[key])
    record["locations"] = _clean(record.get("locations") or [])
    topics = _clean(record.get("topics") or []) or _clean(origin.get("categories") or [])
    record["topics"] = topics or ["local"]
    title = record.get("title")
    if not title:
        return None
    kind = record.get("kind", "news")
    date = record.get("start") or record.get("publishedAt") or ""
    record["id"] = f"{_slug(title)}-{kind}-{_slug(str(date))[:10]}".strip("-")
    record["addedAt"] = datetime.now(timezone.utc).astimezone(dates.EASTERN).isoformat(timespec="seconds")
    record["url"] = record.get("url") or origin.get("url")
    record["imageUrl"] = (
        record.get("imageUrl") or origin.get("page_image") or origin.get("image") or None
    )
    if record.get("imageUrl"):
        record["imageAlt"] = record.get("imageAlt") or title
    else:
        record.pop("imageAlt", None)
    record["source"] = {
        "name": record.get("author") or origin.get("source_name"),
        "publication": record.get("publication") or origin.get("source_name"),
    }
    if "localityIndex" not in record:
        record["localityIndex"] = origin.get("locality_index")
    record["origin"] = {
        "chunk_id": origin.get("chunk_id") or origin.get("item_id"),
        "source_slug": origin.get("source_slug"),
        "url": origin.get("url"),
    }
    return record


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def run(items: list[dict[str, Any]], *, workers: int, extract_limit: int,
        model: Optional[str], enrich_events: bool = True,
        enrich_limit: int = 200) -> tuple[list[dict[str, Any]], dict[str, int]]:
    jev_client = jev.make_client()

    def triage_one(rec: dict[str, Any]) -> tuple[dict[str, Any], jev.Triage]:
        return rec, jev.triage_record(rec, jev_client)

    triaged: list[tuple[dict[str, Any], jev.Triage]] = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for rec, tri in pool.map(triage_one, items):
            triaged.append((rec, tri))
    accepted = [(r, t) for r, t in triaged if t.accepted][:extract_limit]

    chat = llm.make_chat_client(model) if accepted else None
    enrich_lock = threading.Lock()
    enrich_state = {"used": 0, "ok": 0}

    def extract_one(pair: tuple[dict[str, Any], jev.Triage]) -> dict[str, Any]:
        rec, tri = pair
        route = router.route(tri)
        records: list[dict[str, Any]] = []
        if chat is not None:
            try:
                data = chat.complete_json(
                    prompts.build_messages(route.mode, rec, route.record_cap),
                    prompts.schema_for(route.mode),
                    schema_name=prompts.schema_name(route.mode),
                )
                records = prompts.records_from_response(route.mode, data)[: route.record_cap]
            except llm.LLMError:
                records = []
        if route.schema == "event":
            if enrich_events and chat is not None:
                for r in records:
                    if not any(not r.get(f) for f in ("start", "venue", "city")):
                        continue
                    with enrich_lock:
                        allowed = enrich_state["used"] < enrich_limit
                        if allowed:
                            enrich_state["used"] += 1
                    if not allowed:
                        break
                    before = sum(1 for f in ("start", "venue", "city") if r.get(f))
                    enrich.enrich_event(r, chat)
                    if sum(1 for f in ("start", "venue", "city") if r.get(f)) > before:
                        with enrich_lock:
                            enrich_state["ok"] += 1
            # Scoring rode along in the triage call; apply it to this chunk's events.
            event_score = scoring.score_answers(tri.raw)
            for r in records:
                r["score"] = event_score
        return {"origin": rec, "triage": tri, "route": route, "records": records}

    results: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for out in pool.map(extract_one, accepted):
            results.append(out)

    finalized: list[dict[str, Any]] = []
    for out in results:
        for rec in out["records"]:
            clean = finalize(rec, out["origin"])
            if clean:
                finalized.append(clean)
    stats = {
        "selected": len(items),
        "accepted": len(accepted),
        "extracted_records": len(finalized),
        "enriched": enrich_state["ok"],
        "modes": {m: sum(1 for _, t in accepted if router.route(t).mode == m)
                  for m in ("event_single", "event_array", "news_single", "news_array")},
    }
    return finalized, stats


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--max-items", type=int, default=400)
    p.add_argument("--extract-limit", type=int, default=150)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--model", default=None)
    p.add_argument("--reference-date", default=None)
    p.add_argument("--no-enrich", dest="enrich_events", action="store_false", default=True,
                   help="skip the fallback fetch for events missing fields")
    p.add_argument("--enrich-limit", type=int, default=200,
                   help="max event records to enrich with a fetch")
    opts = p.parse_args(argv)

    ref = dates.parse_reference(opts.reference_date)
    candidates = load_candidates(opts.processed)
    selected = select(candidates, opts.max_items, ref)
    print(f"candidates={len(candidates)} selected={len(selected)}")
    records, stats = run(selected, workers=opts.workers, extract_limit=opts.extract_limit,
                         model=opts.model, enrich_events=opts.enrich_events,
                         enrich_limit=opts.enrich_limit)
    opts.out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    print(json.dumps(stats, indent=2))
    print(f"wrote {len(records)} records -> {opts.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())