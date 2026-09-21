#!/usr/bin/env python3
"""Incremental extraction pipeline: processed chunks -> triage -> route -> records.

    .venv/bin/python -m extract.pipeline [--max-items N] [--extract-limit N]

Reads canonical, in-window chunks and feed items from ``processed/`` and turns
the ones most likely to be brief-worthy into schema-shaped records. Two files
under ``processed/`` make the run incremental (stage 9's idempotent store):

* ``extraction_index.jsonl`` — a fingerprint for every candidate that has been
  through the Jev gate (and, when accepted, extraction), keyed by
  ``(source_slug, EXTRACTOR_VERSION, chunk_id/item_id)``. A candidate is
  reprocessed only when its content changes or the extractor is bumped.
* ``extracted_records.jsonl`` — the cumulative record store. New records are
  merged by id (newest wins) and the store is pruned by age;
  ``extract.publish`` reads it exactly as before.

Selection is bounded twice over: candidates whose newest known date is more
than ``--stale-days`` in the past are never triaged at all (a daily brief is
forward-looking), and ``--max-items`` caps Jev calls per run. Because processed
candidates are never re-paid for, the steady-state hourly cost is just the new
candidates that the crawler brought in.

Record ids/addedAt/localityIndex are assigned here, not by the model.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import dates, enrich, jev, llm, prompts, router, scoring
from .funnel import slugify

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROCESSED = SCRIPT_DIR / "processed"
DEFAULT_OUT = SCRIPT_DIR / "processed" / "extracted_records.jsonl"
DEFAULT_INDEX = SCRIPT_DIR / "processed" / "extraction_index.jsonl"
DEFAULT_SOURCE_FILE = SCRIPT_DIR / "source.json"

#: Bump when prompts, routing or extraction logic change meaningfully; doing so
#: re-extracts every candidate once (fingerprints include the version).
EXTRACTOR_VERSION = 1

STALE_DAYS = 2.0          # never triage candidates dated older than this
RECORD_MAX_AGE_DAYS = 183  # prune the store past this age (roughly 6 months)

_CONTENT_HINTS = {"event", "news", "notice", "agenda"}
_NULLISH = {"", "null", "none", "n/a", "unknown", "not specified"}


# --------------------------------------------------------------------------- #
# Loading + incremental state
# --------------------------------------------------------------------------- #

def load_source_specs(source_file: Path) -> dict[str, dict[str, Any]]:
    """slug -> per-source extraction specs from the catalog.

    Includes the auto-derived venue default: an event extracted from a
    Venue-type source happens at that venue unless its text says otherwise,
    so ``default_venue``/``default_city`` are filled from the source name and
    location when not set explicitly.
    """
    specs: dict[str, dict[str, Any]] = {}
    try:
        catalog = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return specs
    for source in catalog.get("sources", []):
        if not isinstance(source, dict) or not source.get("name"):
            continue
        slug = slugify(str(source["name"]))
        spec: dict[str, Any] = {
            "content_mode": source.get("content_mode") or "auto",
            "default_venue": source.get("default_venue"),
            "default_city": source.get("default_city"),
        }
        if source.get("type") == "Venue":
            location = str(source.get("location") or "")
            spec["default_venue"] = spec["default_venue"] or str(source["name"]).strip()
            spec["default_city"] = spec["default_city"] or location.split(",")[0].strip()
        specs[slug] = spec
    return specs


def apply_source_defaults(record: dict[str, Any],
                          spec: Optional[dict[str, Any]]) -> None:
    """Fill a record's missing venue/city from the source defaults (in place)."""
    if not spec:
        return
    if not record.get("venue") and spec.get("default_venue"):
        record["venue"] = spec["default_venue"]
    if not record.get("city") and spec.get("default_city"):
        record["city"] = spec["default_city"]


def load_enrich_hints(source_file: Path) -> dict[str, str]:
    """slug -> manual ``enrich_strategy`` hint from the source catalog."""
    hints: dict[str, str] = {}
    try:
        catalog = json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return hints
    for source in catalog.get("sources", []):
        if isinstance(source, dict) and source.get("name"):
            mode = source.get("enrich_strategy")
            if isinstance(mode, str) and mode in ("auto", "jsonld", "ical", "llm", "url_date", "none"):
                hints[slugify(str(source["name"]))] = mode
    return hints


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


def _fingerprint(rec: dict[str, Any]) -> str:
    cid = rec.get("chunk_id") or rec.get("item_id") or ""
    return f"{rec.get('source_slug')}:{EXTRACTOR_VERSION}:{cid}"


def _store_fingerprint(record: dict[str, Any]) -> Optional[str]:
    """Fingerprint of the candidate a stored record came from."""
    origin = record.get("origin") or {}
    slug, cid = origin.get("source_slug"), origin.get("chunk_id")
    return f"{slug}:{EXTRACTOR_VERSION}:{cid}" if slug and cid else None


def load_index(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            done.add(json.loads(line)["fp"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return done


def append_index(path: Path, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    with path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_store(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _record_age_days(record: dict[str, Any], ref: datetime) -> Optional[float]:
    iso = record.get("start") or record.get("publishedAt") or record.get("addedAt")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dates.EASTERN)
    return (dt - ref).total_seconds() / 86400


def _prune_store(records: list[dict[str, Any]], ref: datetime,
                 max_age_days: float) -> list[dict[str, Any]]:
    kept = []
    for record in records:
        age = _record_age_days(record, ref)
        if age is None or age > -max_age_days:
            kept.append(record)
    return kept


# --------------------------------------------------------------------------- #
# Selection: date-sorted, stale-excluded
# --------------------------------------------------------------------------- #

def candidate_days(rec: dict[str, Any], ref: datetime) -> Optional[float]:
    """Signed days from *ref* to the candidate's newest known date.

    Feed items carry their publish (RSS) or event (ICS) date as ``age_days``;
    dates scanned from the text sit in ``signals.date_spans``. ``None`` means
    undated (kept: the Jev gate decides those).
    """
    days: list[float] = []
    age = rec.get("age_days")
    if isinstance(age, (int, float)):
        days.append(float(age))
    for span in (rec.get("signals") or {}).get("date_spans") or []:
        iso = span.get("iso") if isinstance(span, dict) else None
        if not iso:
            continue
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dates.EASTERN)
        days.append((dt - ref).total_seconds() / 86400)
    return max(days) if days else None


def priority(rec: dict[str, Any], ref: datetime) -> tuple:
    """Nearest-to-today first: the brief cares about now and the near future.
    Undated candidates sort last; feeds beat chunks on ties (mechanical)."""
    days = candidate_days(rec, ref)
    return (
        1 if days is None else 0,
        abs(days) if days is not None else 999.0,
        0 if rec["_kind"] == "feed" else 1,
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
        enrich_limit: int = 200,
        enrich_hints: Optional[dict[str, str]] = None,
        source_specs: Optional[dict[str, dict[str, Any]]] = None) -> tuple[list[dict[str, Any]], dict[str, int],
                                          list[tuple[dict[str, Any], str]]]:
    """Triage + extract ``items``; return (records, stats, processed).

    ``processed`` is a list of ``(candidate, outcome)`` for the candidates that
    were fully handled this run and may be fingerprinted: ``"rejected"`` by the
    Jev gate, or ``"extracted"`` (accepted, within the limit). Accepted
    candidates beyond ``--extract-limit`` are *not* included — they stay
    un-fingerprinted and are retried on a later run.
    """
    jev_client = jev.make_client()

    def triage_one(rec: dict[str, Any]) -> tuple[dict[str, Any], jev.Triage]:
        return rec, jev.triage_record(rec, jev_client)

    triaged: list[tuple[dict[str, Any], jev.Triage]] = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for rec, tri in pool.map(triage_one, items):
            triaged.append((rec, tri))

    processed: list[tuple[dict[str, Any], str]] = []
    accepted: list[tuple[dict[str, Any], jev.Triage]] = []
    deferred = 0
    for rec, tri in triaged:
        if not tri.accepted:
            processed.append((rec, "rejected"))
        elif len(accepted) < extract_limit:
            accepted.append((rec, tri))
            processed.append((rec, "extracted"))
        else:
            deferred += 1  # leave un-fingerprinted; retried next run

    chat = llm.make_chat_client(model) if accepted else None
    enrich_lock = threading.Lock()
    enrich_state = {"used": 0, "ok": 0}
    hints = enrich_hints or {}
    learned = enrich.learned_strategies()

    def extract_one(pair: tuple[dict[str, Any], jev.Triage]) -> dict[str, Any]:
        rec, tri = pair
        route = router.route(tri, content_mode=(source_specs or {}).get(
            rec.get("source_slug") or "", {}).get("content_mode", "auto"))
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
                # Richer sources first: ticketing / FB-event / ical links found
                # in the candidate's own text often carry the date/venue the
                # article page lacks.
                links = enrich.detail_links(rec)
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
                    slug = rec.get("source_slug") or ""
                    enrich.enrich_event(
                        r, chat, detail_links=links,
                        strategy=hints.get(slug) or learned.get(slug) or "auto",
                        source_slug=slug)
                    if sum(1 for f in ("start", "venue", "city") if r.get(f)) > before:
                        with enrich_lock:
                            enrich_state["ok"] += 1
        # Jev triage answered the 12 scoring Nouls in the same call. Persist the
        # per-question probabilities on every record so the weights can be
        # re-tuned from stored data; the composite score is event-only.
        event_score = scoring.score_answers(tri.raw)
        content_flags = {
            "is_lottery": bool(tri.is_lottery),
            "lotteryConfidence": round(float(tri.lottery_conf), 4),
            "is_sports": bool(tri.is_sports),
            "sportsConfidence": round(float(tri.sports_conf), 4),
        }
        for r in records:
            r["contentFlags"] = content_flags
            if route.schema == "event":
                r["score"] = event_score
                r["scoring"] = scoring.detail(tri.raw, event_score)
            else:
                r["scoring"] = scoring.detail(tri.raw)
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
                apply_source_defaults(clean, (source_specs or {}).get(
                    (clean.get("origin") or {}).get("source_slug") or ""))
                finalized.append(clean)
    stats = {
        "selected": len(items),
        "accepted": len(accepted),
        "deferred": deferred,
        "rejected": len(processed) - len(accepted),
        "extracted_records": len(finalized),
        "enriched": enrich_state["ok"],
        "modes": {m: sum(1 for _, t in accepted if router.route(t).mode == m)
                  for m in ("event_single", "event_array", "news_single", "news_array")},
    }
    return finalized, stats, processed


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-file", type=Path, default=DEFAULT_SOURCE_FILE,
                   help="source catalog for per-source detail_parse hints")
    p.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help="cumulative record store (merged, pruned)")
    p.add_argument("--index", type=Path, default=DEFAULT_INDEX,
                   help="fingerprint index of processed candidates")
    p.add_argument("--max-items", type=int, default=2000,
                   help="Jev-triage cap per run (default 2000)")
    p.add_argument("--extract-limit", type=int, default=800,
                   help="generation-call cap per run (default 800)")
    p.add_argument("--stale-days", type=float, default=STALE_DAYS,
                   help="never triage candidates dated older than this (default 2)")
    p.add_argument("--record-max-age-days", type=float, default=RECORD_MAX_AGE_DAYS,
                   help="prune the record store past this age (default 183)")
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
    done = load_index(opts.index)
    store = load_store(opts.out)

    if not done and store:
        # Bootstrap the index from a pre-incremental store so the candidates
        # behind those records are never paid for twice.
        fps = sorted({fp for fp in (_store_fingerprint(r) for r in store) if fp})
        append_index(opts.index, [{"fp": fp, "ts": "", "outcome": "bootstrapped"} for fp in fps])
        done = set(fps)

    fresh: list[dict[str, Any]] = []
    skipped_done = skipped_stale = 0
    for rec in candidates:
        if _fingerprint(rec) in done:
            skipped_done += 1
            continue
        days = candidate_days(rec, ref)
        if days is not None and days < -opts.stale_days:
            skipped_stale += 1
            continue
        fresh.append(rec)
    selected = select(fresh, opts.max_items, ref)
    print(f"candidates={len(candidates)} done={skipped_done} stale={skipped_stale} "
          f"fresh={len(fresh)} selected={len(selected)} store={len(store)}")
    if not selected:
        print("nothing to do")
        return 0

    records, stats, processed = run(selected, workers=opts.workers,
                                    extract_limit=opts.extract_limit,
                                    model=opts.model, enrich_events=opts.enrich_events,
                                    enrich_limit=opts.enrich_limit,
                                    enrich_hints=load_enrich_hints(opts.source_file),
                                    source_specs=load_source_specs(opts.source_file))

    # Merge into the cumulative store (newest wins by id), then prune by age.
    by_id = {r.get("id"): r for r in store if r.get("id")}
    for record in records:
        by_id[record["id"]] = record
    kept = _prune_store(list(by_id.values()), ref, opts.record_max_age_days)
    kept.sort(key=lambda r: (r.get("kind") or "", r.get("id") or ""))
    opts.out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
        encoding="utf-8",
    )
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    append_index(opts.index, [{"fp": _fingerprint(r), "ts": stamp, "outcome": outcome}
                              for r, outcome in processed])
    print(json.dumps(stats, indent=2))
    print(f"store: {len(store)} + {len(records)} new -> {len(kept)} records -> {opts.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
