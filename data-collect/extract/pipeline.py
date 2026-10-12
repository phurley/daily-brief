#!/usr/bin/env python3
"""Incremental extraction pipeline: processed chunks -> triage -> route -> records.

    .venv/bin/python -m extract.pipeline [--max-items N] [--extract-limit N]

Reads canonical, in-window chunks and feed items from ``processed/`` and turns
the ones most likely to be brief-worthy into schema-shaped records. Two files
under ``processed/`` make the run incremental (stage 9's idempotent store):

* ``extraction_index.jsonl`` — a fingerprint for every candidate that has been
  through the Jev gate (and, when accepted, extraction), keyed by
  ``(source_slug, EXTRACTOR_VERSION, chunk_id/item_id)``. A candidate is
  reprocessed when its content changes, the extractor is bumped, or a bounded
  retry is due. Only validated output earns an ``extracted`` outcome.
* ``extracted_records.jsonl`` — the cumulative record store. New records are
  merged by id (newest wins) and the store is pruned by age;
  ``extract.publish`` reads it exactly as before.

Selection is bounded twice over: candidates whose newest known date is more
than ``--stale-days`` in the past are never triaged at all (a daily brief is
forward-looking), and ``--max-items`` caps Jev calls per run. Because processed
candidates are not re-paid for after success, and retries have separate limits.
Recent legacy failures can be replayed from archived source candidates.

Record ids/addedAt/localityIndex are assigned here, not by the model.
"""

from __future__ import annotations

import argparse
import copy
import concurrent.futures as futures
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import dates, enrich, identity, jev, llm, prompts, publish, retry, router, scoring, urls
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
            "identity_authority": source.get("identity_authority") or ("organizer" if source.get("type") == "Venue" else "unknown"),
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
    base = f"{rec.get('source_slug')}:{EXTRACTOR_VERSION}:{cid}"
    if rec.get("_kind") == "feed":
        content = {k: rec.get(k) for k in ("title", "summary", "content", "url", "start", "end",
                                          "venue", "status", "recurrenceId", "sourceUpdatedAt")}
        return base + ":" + identity.digest(content)
    return base


def _store_fingerprint(record: dict[str, Any]) -> Optional[str]:
    """Fingerprint of the candidate a stored record came from."""
    origin = record.get("origin") or {}
    if origin.get("fingerprint"):
        return origin["fingerprint"]
    slug, cid = origin.get("source_slug"), origin.get("chunk_id")
    return f"{slug}:{EXTRACTOR_VERSION}:{cid}" if slug and cid else None


def load_index(path: Path) -> set[str]:
    return {fp for fp, state in retry.load_states(path).items()
            if state.get("outcome") in retry.TERMINAL}


def append_index(path: Path, entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
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


def valid_record(record: dict[str, Any]) -> bool:
    """Use the publisher's contract, without its presentation age filter."""
    if record.get("kind") == "event":
        event = publish.to_event(record)
        return bool(event and not publish._event_date_errors(event)
                    and publish._validator("events.schema.json", "event").is_valid(event))
    if record.get("kind") == "news":
        story = publish.to_story(record)
        return bool(story and publish._validator("news.schema.json", "story").is_valid(story))
    return False


def previous_outputs(store: list[dict], candidates: list[dict]) -> dict[str, list[dict]]:
    """Join legacy provenance conservatively; new records carry exact fingerprints."""
    exact, legacy = {}, {}
    for record in store:
        origin = record.get("origin") or {}
        if origin.get("fingerprint"):
            exact.setdefault(origin["fingerprint"], []).append(record)
        else:
            legacy.setdefault((origin.get("source_slug"), origin.get("chunk_id")), []).append(record)
    return {_fingerprint(c): exact.get(_fingerprint(c), []) + legacy.get(
        (c.get("source_slug"), c.get("chunk_id") or c.get("item_id")), []) for c in candidates}


def select_work(candidates, states, archived, store, ref, *, max_items, retry_limit,
                max_attempts, replay_days, stale_days, reextract_sources=()):
    """Reserve a bounded share for due retries/recent old failures; keep fresh work moving."""
    all_candidates = {}
    for row in archived.values():
        c = row.get("candidate")
        if c and row.get("fp") in states and row.get("fp") == _fingerprint(c):
            # age_days is relative to an old crawl. Absolute dates below remain
            # usable; undated archived candidates are bounded by index age.
            c = dict(c)
            c.pop("age_days", None)
            all_candidates[row["fp"]] = c
    all_candidates.update({_fingerprint(c): c for c in candidates})
    previous = previous_outputs(store, list(all_candidates.values()))
    fresh, pending = [], []
    for fp, c in all_candidates.items():
        if not c.get("in_window", True) or not c.get("is_canonical", True):
            continue
        if (c.get("signals") or {}).get("locality", {}).get("out_of_area"):
            continue
        days = candidate_days(c, ref)
        # Feed start/publish dates survive archive replay even without date_spans.
        absolute = retry.timestamp(c.get("start") or c.get("publishedAt"))
        if absolute is not None:
            days = max(days if days is not None else -float("inf"), (absolute - ref).total_seconds() / 86400)
        if days is not None and days < -stale_days:
            continue
        state = states.get(fp, {})
        if c.get("source_slug") in reextract_sources or not state:
            fresh.append(c)
        elif retry.due(state, ref, max_attempts):
            pending.append(c)
        elif retry.legacy_replayable(state, ref, replay_days):
            outputs = previous[fp]
            event_hint = c.get("candidate_hint") == "event" or c.get("feed_kind") in {"ics", "jsonld"}
            if (outputs and any(r.get("kind") == "event" and not valid_record(r) for r in outputs)
                    or not outputs and event_hint):
                pending.append(c)
    repairs = select(pending, min(retry_limit, max_items), ref)
    selected = repairs + select(fresh, max_items - len(repairs), ref)
    return selected, previous, {"fresh": len(fresh), "retry_due": len(pending), "retry_selected": len(repairs)}


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
        "fingerprint": _fingerprint(origin),
    }
    if kind == "event":
        # Structured feed fields are source evidence, not model-generated IDs.
        if origin.get("feed_kind") in ("ics", "jsonld"):
            for key in ("sourceEventId", "recurrenceId", "recurrenceRule", "sourceUpdatedAt",
                        "start", "end", "venue", "timePrecision", "status", "statusEvidence", "previousStart"):
                if origin.get(key):
                    record[key] = origin[key]
        evidence = record.get("statusEvidence")
        source_text = jev.record_text(origin)
        if evidence and evidence not in source_text and evidence != origin.get("statusEvidence"):
            record.pop("statusEvidence", None)
            record.pop("status", None)
        record["observedAt"] = record["addedAt"]
        record["sourceId"] = origin.get("source_slug") or ""
        record = identity.prepare(record)
        record["aliases"] = [record["id"]]
        record["id"] = record["sourceRecordId"]
    return record


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #

def structured_event(candidate, spec=None):
    """Use a complete structured feed as the base; incomplete feeds use extraction."""
    if candidate.get("feed_kind") not in ("ics", "jsonld"):
        return None
    city = candidate.get("city") or (spec or {}).get("default_city")
    if not city or not all(candidate.get(k) for k in ("title", "summary", "start", "venue", "url")):
        return None
    result = {k: candidate[k] for k in ("title", "summary", "start", "end", "venue", "url",
              "status", "statusEvidence", "timePrecision", "sourceEventId", "recurrenceId",
              "recurrenceRule", "sourceUpdatedAt", "previousStart") if candidate.get(k)}
    return dict(result, kind="event", city=city, category=candidate.get("category") or "Event")


def run(items: list[dict[str, Any]], *, workers: int, extract_limit: int,
        model: Optional[str], enrich_events: bool = True,
        enrich_limit: int = 200,
        enrich_hints: Optional[dict[str, str]] = None,
        source_specs: Optional[dict[str, dict[str, Any]]] = None,
        previous: Optional[dict[str, list[dict]]] = None) -> tuple[list[dict[str, Any]], dict[str, int],
                                          list[tuple[dict[str, Any], str]]]:
    """Triage + extract ``items``; return (records, stats, processed).

    ``processed`` contains terminal ``rejected``/validated ``extracted`` or
    ``retry:<reason>`` outcomes. Accepted
    candidates beyond ``--extract-limit`` are *not* included — they stay
    un-fingerprinted and are retried on a later run.
    """
    jev_client = jev.make_client()

    def triage_one(rec: dict[str, Any]):
        try:
            return rec, jev.triage_record(rec, jev_client)
        except (jev.JevError, TimeoutError, OSError):
            return rec, None

    triaged: list[tuple[dict[str, Any], jev.Triage]] = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for rec, tri in pool.map(triage_one, items):
            triaged.append((rec, tri))

    processed: list[tuple[dict[str, Any], str]] = []
    accepted: list[tuple[dict[str, Any], jev.Triage]] = []
    deferred = 0
    for rec, tri in triaged:
        if tri is None:
            processed.append((rec, "retry:triage_failure"))
        elif not tri.accepted:
            processed.append((rec, "rejected"))
        elif len(accepted) < extract_limit:
            accepted.append((rec, tri))
        else:
            deferred += 1  # leave un-fingerprinted; retried next run

    chat = llm.make_chat_client(model) if accepted else None
    enrich_lock = threading.Lock()
    enrich_state = {"used": 0, "ok": 0, "mechanical": 0, "repair": 0, "generation": 0}
    hints = enrich_hints or {}
    learned = enrich.learned_strategies()

    def extract_one(pair: tuple[dict[str, Any], jev.Triage]) -> dict[str, Any]:
        rec, tri = pair
        route = router.route(tri, content_mode=(source_specs or {}).get(
            rec.get("source_slug") or "", {}).get("content_mode", "auto"))
        records: list[dict[str, Any]] = []
        reason = None
        repair_records = [copy.deepcopy(r) for r in (previous or {}).get(_fingerprint(rec), [])
                          if r.get("kind") == "event" and not valid_record(r)]
        if any(not all(r.get(k) for k in ("title", "summary", "url")) for r in repair_records):
            repair_records = []  # Touch-up cannot supply these; re-extract the source text.
        if repair_records:
            # These are already extracted events. Repair their own detail pages,
            # preserving good fields and previously collected sibling records.
            route = router.Route("extract", "event", "event_array", len(repair_records), "repair")
        base = structured_event(rec, (source_specs or {}).get(rec.get("source_slug"))) if route.schema == "event" else None
        if repair_records:
            records = repair_records
            with enrich_lock:
                enrich_state["repair"] += 1
        elif base:
            records = [base]
            with enrich_lock:
                enrich_state["mechanical"] += 1
        elif chat is not None:
            with enrich_lock:
                enrich_state["generation"] += 1
            try:
                data = chat.complete_json(
                    prompts.build_messages(route.mode, rec, route.record_cap),
                    prompts.schema_for(route.mode),
                    schema_name=prompts.schema_name(route.mode),
                )
                records = prompts.records_from_response(route.mode, data)[: route.record_cap]
            except (llm.LLMError, ValueError, TypeError, KeyError):
                records = []
                reason = "generation_failure"
        usable = [r for r in records if all(r.get(k) is None or isinstance(r[k], str)
                  for k in ("title", "summary", "url", "start", "end", "venue", "city", "category", "publishedAt"))]
        if len(usable) != len(records):
            reason = "invalid_output"
        records = usable
        for r in records:
            # Select a matching event detail link BEFORE enrichment. Listing-wide
            # ticket links can belong to another event in the same chunk.
            page_url = rec.get("url")
            if not r.get("url") or r.get("url") == page_url:
                detail = urls.title_link(rec.get("text"), r.get("title"), page_url=page_url)
                if detail:
                    r["url"] = detail
            apply_source_defaults(r, (source_specs or {}).get(rec.get("source_slug")))
            if route.schema == "event":
                for key in ("start", "end"):
                    if r.get(key) and not publish._valid_event_datetime(publish._normalize_midnight(r[key])):
                        r.pop(key, None)
        if route.schema == "event":
            if enrich_events and chat is not None:
                # Richer sources first: ticketing / FB-event / ical links found
                # in the candidate's own text often carry the date/venue the
                # article page lacks.
                for r in records:
                    if not any(not r.get(f) for f in ("start", "venue", "city", "category")):
                        continue
                    with enrich_lock:
                        allowed = enrich_state["used"] < enrich_limit
                        if allowed:
                            enrich_state["used"] += 1
                    if not allowed:
                        break
                    before = sum(1 for f in ("start", "venue", "city") if r.get(f))
                    slug = rec.get("source_slug") or ""
                    strategy = hints.get(slug) or ("auto" if repair_records else learned.get(slug)) or "auto"
                    enrich.enrich_event(
                        r, chat, detail_links=enrich.detail_links(r),
                        strategy=strategy, source_slug=slug)
                    if sum(1 for f in ("start", "venue", "city") if r.get(f)) > before:
                        with enrich_lock:
                            enrich_state["ok"] += 1
        if any(r.get("_repair_conflict") for r in records):
            reason = "source_date_conflict"
            # Keep the original incomplete snapshot, but do not promote an
            # attempted repair that contradicts its own matched source page.
            records = [r for r in records if not r.get("_repair_conflict")]
        # Score each record from its OWN fields, not the whole chunk, so a single
        # sporting event inside a mixed listing registers its own ``sporting``
        # signal. Triage still supplies the content flags; ``tri.raw`` is the
        # fallback if the scoring call fails (scoring is best-effort).
        content_flags = {
            "is_lottery": bool(tri.is_lottery),
            "lotteryConfidence": round(float(tri.lottery_conf), 4),
            "is_sports": bool(tri.is_sports),
            "sportsConfidence": round(float(tri.sports_conf), 4),
        }
        is_event = route.schema == "event"
        for r in records:
            r.setdefault("kind", route.schema)
            if is_event and r.get("timePrecision") == "unknown" and publish._valid_event_datetime(r.get("start")):
                r.pop("timePrecision")  # Recompute now that repair supplied a real time.
            r["contentFlags"] = content_flags
            try:
                _, answers = scoring.score_record(r, jev_client, kind=route.schema)
            except Exception:  # noqa: BLE001 - scoring is best-effort
                answers = tri.raw
            breakdown = scoring.detail(answers, with_score=is_event)
            r["scoring"] = breakdown
            if is_event:
                r["score"] = breakdown["score"]
            # On a listing page the model often inherits the page URL; prefer the
            # detail link on the item's own title when one is present.
            page_url = rec.get("url")
            if not r.get("url") or r.get("url") == page_url:
                detail = urls.title_link(rec.get("text"), r.get("title"), page_url=page_url)
                if detail:
                    r["url"] = detail
        return {"origin": rec, "triage": tri, "route": route, "records": records, "reason": reason}

    results: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for out in pool.map(extract_one, accepted):
            results.append(out)

    finalized: list[dict[str, Any]] = []
    for out in results:
        complete = bool(out["records"]) and out["reason"] is None
        for rec in out["records"]:
            clean = finalize(rec, out["origin"])
            if clean:
                apply_source_defaults(clean, (source_specs or {}).get(
                    (clean.get("origin") or {}).get("source_slug") or ""))
                if clean.get("kind") == "event":
                    clean["sourceAuthority"] = (source_specs or {}).get(
                        clean.get("sourceId"), {}).get("identity_authority", "unknown")
                    # Defaults must participate in snapshot identity.
                    clean.pop("sourceRecordId", None)
                    clean.pop("venueId", None)
                    clean = identity.prepare(clean)
                    clean["id"] = clean["sourceRecordId"]
                finalized.append(clean)
                complete = valid_record(clean) and complete
            else:
                complete = False
        outcome = "extracted" if complete else "retry:" + (out["reason"] or (
            "incomplete" if out["records"] else "empty_output"))
        processed.append((out["origin"], outcome))
    stats = {
        "selected": len(items),
        "mechanical_feeds": enrich_state["mechanical"],
        "generation_calls": enrich_state["generation"],
        "repair_candidates": enrich_state["repair"],
        "accepted": len(accepted),
        "deferred": deferred,
        "rejected": sum(outcome == "rejected" for _, outcome in processed),
        "retryable": sum(outcome.startswith("retry:") for _, outcome in processed),
        "completed": sum(outcome == "extracted" for _, outcome in processed),
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
    p.add_argument("--reextract-source", action="append", default=[],
                   help="targeted source slug refresh; still bounded by --max-items/--extract-limit")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--model", default=None)
    p.add_argument("--reference-date", default=None)
    p.add_argument("--no-enrich", dest="enrich_events", action="store_false", default=True,
                   help="skip the fallback fetch for events missing fields")
    p.add_argument("--enrich-limit", type=int, default=200,
                   help="max event records to enrich with a fetch")
    p.add_argument("--retry-limit", type=int, default=50,
                   help="reserve at most this many candidate slots for retries/replay (default 50)")
    p.add_argument("--max-attempts", type=int, default=3,
                   help="attempts before retaining a failed candidate as exhausted (default 3)")
    p.add_argument("--retry-backoff-hours", type=float, default=1,
                   help="first retry delay; subsequent delay is 4x, capped at 24h")
    p.add_argument("--replay-days", type=float, default=14,
                   help="look back this many days for old extracted-but-incomplete events; 0 disables")
    p.add_argument("--dry-run", action="store_true", help="show selection without model calls or writes")
    opts = p.parse_args(argv)
    if (min(opts.max_items, opts.extract_limit, opts.retry_limit, opts.enrich_limit) < 0
            or opts.max_attempts < 1 or opts.retry_backoff_hours <= 0 or opts.replay_days < 0):
        p.error("limits must be nonnegative; attempts and backoff must be positive")

    ref = dates.parse_reference(opts.reference_date)
    candidates = load_candidates(opts.processed)
    states = retry.load_states(opts.index)
    store = load_store(opts.out)
    candidate_archive = opts.out.with_name("source_candidate_archive.jsonl")
    archived_candidates = retry.load_states(candidate_archive)
    if not states and store:
        # Bootstrap the index from a pre-incremental store so the candidates
        # behind those records are never paid for twice.
        outputs = previous_outputs(store, candidates)
        states = {fp: {"fp": fp, "outcome": "bootstrapped"} for fp, records in outputs.items()
                  if records and all(valid_record(r) for r in records)}
        if not opts.dry_run:
            append_index(opts.index, list(states.values()))

    selected, previous, selection = select_work(
        candidates, states, archived_candidates, store, ref, max_items=opts.max_items,
        retry_limit=min(opts.retry_limit, opts.extract_limit), max_attempts=opts.max_attempts,
        replay_days=opts.replay_days, stale_days=opts.stale_days,
        reextract_sources=opts.reextract_source)
    print(f"candidates={len(candidates)} fresh={selection['fresh']} "
          f"retry_due={selection['retry_due']} retry_selected={selection['retry_selected']} "
          f"selected={len(selected)} store={len(store)}", flush=True)
    if opts.dry_run:
        print("(dry run, no calls or writes)")
        return 0
    if not selected:
        print("nothing to do")
        return 0

    # Preserve original candidate text and feed metadata for bounded replay,
    # independently of mutable crawl/processed files and extracted snapshots.
    opts.out.parent.mkdir(parents=True, exist_ok=True)
    opts.index.parent.mkdir(parents=True, exist_ok=True)
    append_index(candidate_archive, [
        {"fp": _fingerprint(r), "candidate": r} for r in selected
        if _fingerprint(r) not in archived_candidates
    ])

    records, stats, processed = run(selected, workers=opts.workers,
                                    extract_limit=opts.extract_limit,
                                    model=opts.model, enrich_events=opts.enrich_events,
                                    enrich_limit=opts.enrich_limit,
                                    enrich_hints=load_enrich_hints(opts.source_file),
                                    source_specs=load_source_specs(opts.source_file), previous=previous)

    # Preserve event source snapshots independently of presentation IDs. Never
    # overwrite separate sessions or source evidence sharing a legacy title ID.
    def store_key(r):
        return identity.prepare(r)["sourceRecordId"] if r.get("kind") == "event" else r.get("id")
    by_id = {store_key(r): r for r in store if r.get("id")}
    for record in records:
        by_id[store_key(record)] = record
    archive = opts.out.with_name("event_source_archive.jsonl")
    archived = {r.get("sourceRecordId") for r in load_store(archive)}
    with archive.open("a", encoding="utf-8") as fh:
        for r in by_id.values():
            if r.get("kind") == "event":
                snapshot = identity.prepare(r)
                if snapshot["sourceRecordId"] not in archived:
                    fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
                    archived.add(snapshot["sourceRecordId"])
    kept = _prune_store(list(by_id.values()), ref, opts.record_max_age_days)
    kept.sort(key=lambda r: (r.get("kind") or "", r.get("id") or ""))
    temporary = opts.out.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
        encoding="utf-8",
    )
    temporary.replace(opts.out)
    stamp = datetime.now(timezone.utc)
    outputs = previous_outputs(records, [r for r, _ in processed])
    entries = [retry.entry(_fingerprint(r), outcome, states.get(_fingerprint(r), {}), stamp,
                           max_attempts=opts.max_attempts, backoff_hours=opts.retry_backoff_hours,
                           record_ids=[v["id"] for v in outputs[_fingerprint(r)] if valid_record(v)])
               for r, outcome in processed]
    append_index(opts.index, entries)
    stats["exhausted"] = sum(e["outcome"] == "exhausted" for e in entries)
    stats.update(selection)
    print(json.dumps(stats, indent=2))
    print(f"store: {len(store)} + {len(records)} new -> {len(kept)} records -> {opts.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
