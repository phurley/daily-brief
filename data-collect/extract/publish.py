#!/usr/bin/env python3
"""Stage 8/9: validate extracted records and publish events.json / news.json.

    .venv/bin/python -m extract.publish --records processed/extracted_records.jsonl
    .venv/bin/python -m extract.publish --records ... --out /tmp --dry-run

Each record is normalized and validated against the root schemas
(``schemas/events.schema.json`` / ``schemas/news.schema.json``) *before* it is
added, and the finished document is validated again before writing. Records that
cannot meet the schema are dropped and counted; we never write an invalid file.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import dates, jev

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent.parent
SCHEMAS = ROOT / "schemas"
DEFAULT_RECORDS = ROOT / "data-collect" / "processed" / "extracted_records.jsonl"

EVENT_REQUIRED = ("id", "title", "url", "start", "venue", "city", "category", "summary")
EVENT_OPTIONAL = ("end", "region", "price", "ageRestriction", "registration",
                  "imageUrl", "imageAlt", "deadline")
STORY_REQUIRED = ("id", "title", "summary", "url", "publishedAt", "addedAt",
                  "localityIndex", "locations", "topics", "source")
_NULLISH = {"", "null", "none", "n/a", "unknown", "not specified", "tbd"}


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return None if value.strip().lower() in _NULLISH else value.strip()
    if isinstance(value, list):
        out = []
        for v in value:
            c = _clean(v)
            if c:
                out.append(c)
        return out
    return value


def _valid_id(value: Any) -> Optional[str]:
    if not value:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug or None


def _scoring(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Pass through the stored Jev scoring breakdown, sanitized for the schema.

    Shape: ``{"score": 0-100?, "signals": {question: P(yes)}}``. Returns None
    when there is nothing usable so the field is simply omitted.
    """
    stored = record.get("scoring")
    if not isinstance(stored, dict):
        return None
    signals = stored.get("signals")
    clean = {
        str(k): max(0.0, min(1.0, float(v)))
        for k, v in (signals or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if not clean:
        return None
    out: dict[str, Any] = {"signals": clean}
    score = stored.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        out["score"] = max(0, min(100, int(score)))
    return out


def _content_dropped(record: dict[str, Any]) -> bool:
    """True when the triage content flags say this is lottery or sports.

    Defense-in-depth for records that predate the drops or slipped through a
    concurrent run; new candidates are dropped at the gate in ``jev.py``.
    """
    flags = record.get("contentFlags")
    if not isinstance(flags, dict):
        return False
    if flags.get("is_lottery") and float(flags.get("lotteryConfidence") or 0.0) >= jev.LOTTERY_DROP_CONFIDENCE:
        return True
    if flags.get("is_sports") and float(flags.get("sportsConfidence") or 0.0) >= jev.SPORTS_DROP_CONFIDENCE:
        return True
    return False


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=dates.EASTERN)


def to_event(record: dict[str, Any], cutoff: Optional[datetime] = None,
             edition: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    if record.get("kind") != "event":
        return None
    event: dict[str, Any] = {}
    event["id"] = _valid_id(record.get("id"))
    for key in ("title", "summary", "url", "start", "end", "venue", "city", "region",
                "price", "ageRestriction", "registration", "category",
                "imageUrl", "imageAlt", "deadline"):
        value = _clean(record.get(key))
        if value:
            event[key] = value
    if isinstance(record.get("score"), (int, float)):
        event["score"] = max(0, min(100, int(record["score"])))
    scoring = _scoring(record)
    if scoring:
        event["scoring"] = scoring
    if event.get("imageUrl"):
        event.setdefault("imageAlt", event.get("title"))
    else:
        event.pop("imageUrl", None)
        event.pop("imageAlt", None)
    if any(not event.get(k) for k in EVENT_REQUIRED):
        return None
    if cutoff is not None:
        # Forward-looking: an event is stale only when it is over. With an
        # end date, that means the end passed before the edition day (a
        # running exhibition with an old start is still plannable);
        # start-only events get the max_event_age_days grace window.
        start = _parse_dt(event.get("start"))
        end = _parse_dt(event.get("end"))
        if end is not None:
            edition_day = edition.date() if edition is not None else cutoff.date()
            if end.date() < edition_day:
                return None
        elif start is not None and start < cutoff:
            return None  # stale event; brief is forward-looking
    return event


def to_story(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    if record.get("kind") != "news":
        return None
    story: dict[str, Any] = {}
    story["id"] = _valid_id(record.get("id"))
    for key in ("title", "summary", "url", "publishedAt"):
        value = _clean(record.get(key))
        if value:
            story[key] = value
    story["addedAt"] = _clean(record.get("addedAt")) or datetime.now(dates.EASTERN).isoformat(timespec="seconds")
    if isinstance(record.get("localityIndex"), int):
        story["localityIndex"] = record["localityIndex"]
    story["locations"] = _clean(record.get("locations") or [])
    topics = _clean(record.get("topics") or [])
    story["topics"] = topics or ["local"]
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    name = _clean(source.get("name")) or _clean(record.get("publication"))
    if name:
        story["source"] = {"name": name}
        pub = _clean(source.get("publication")) or _clean(record.get("publication"))
        if pub and pub != name:
            story["source"]["publication"] = pub
    scoring = _scoring(record)
    if scoring:
        story["scoring"] = scoring
    image = _clean(record.get("imageUrl"))
    if image:
        photo = {"url": image, "alt": _clean(record.get("imageAlt")) or story.get("title")}
        story["photo"] = photo
    if any(story.get(k) in (None, "", []) for k in ("id", "title", "summary", "url",
                                                     "publishedAt", "addedAt", "source")):
        return None
    if "localityIndex" not in story:
        return None
    return story


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _same_title(a: str, b: str) -> bool:
    """True when two titles name the same thing: equal after normalization, or
    one is the other's base title with a subtitle separator (so
    ``Witches Night Bazaar: Autumnal Equinox`` matches ``Witches Night Bazaar``)."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short_raw, long_raw = (a, b) if len(na) <= len(nb) else (b, a)
    if not _norm_title(long_raw).startswith(_norm_title(short_raw) + " "):
        return False
    idx = long_raw.lower().find(short_raw.lower())
    if idx < 0:
        return False  # punctuation differs too much to be a safe subtitle match
    rest = long_raw[idx + len(short_raw):]
    return rest.lstrip()[:1] in (":", "|", "\u2014", "\u2013", "-")


def _dedupe_day(record: dict[str, Any]) -> str:
    return str(record.get("start") or record.get("publishedAt") or "")[:10]


def _compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two same-kind records are the same item and may be merged.

    Exact normalized titles always match. A base/subtitle pair (``Witches Night
    Bazaar`` vs ``Witches Night Bazaar: Autumnal Equinox``) only matches when it
    is really the same happening: a shared real date, no conflicting venue, and
    start times within a few hours. This keeps distinct sessions of a series
    (Dolly Day films, SynthFest workshops, per-branch voter drives) apart.
    """
    na, nb = _norm_title(a.get("title") or ""), _norm_title(b.get("title") or "")
    if not na or not nb:
        return False
    if na == nb:
        return True
    if not _same_title(a.get("title") or "", b.get("title") or ""):
        return False
    day_a, day_b = _dedupe_day(a), _dedupe_day(b)
    if not day_a or day_a != day_b:
        return False
    venue_a = _norm_title(a.get("venue") or "")
    venue_b = _norm_title(b.get("venue") or "")
    if venue_a and venue_b and venue_a != venue_b:
        return False
    start_a, start_b = _parse_dt(a.get("start")), _parse_dt(b.get("start"))
    if start_a and start_b and abs((start_a - start_b).total_seconds()) > 4 * 3600:
        return False
    return True


def _richness(record: dict[str, Any]) -> tuple:
    """Sortable completeness so a duplicate pair keeps the better record."""
    score = record.get("score")
    has_score = isinstance(score, (int, float)) and not isinstance(score, bool)
    return (
        1 if record.get("imageUrl") else 0,
        1 if has_score else 0,
        int(score) if has_score else 0,
        len(record.get("summary") or ""),
        sum(1 for f in ("venue", "city", "region", "price", "registration",
                        "category", "end", "publishedAt", "deadline") if record.get(f)),
        len(record.get("topics") or []),
    )


def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-kind records on the same day whose titles name the same
    event, keeping the richest one (not merely the first)."""
    best: dict[tuple, dict[str, Any]] = {}
    order: list[tuple] = []
    by_day: dict[tuple, list[tuple]] = {}
    for r in records:
        kind = r.get("kind")
        day = _dedupe_day(r)
        key = (kind, day, _norm_title(r.get("title") or ""))
        bucket = by_day.setdefault((kind, day), [])
        match = key if key in best else None
        if match is None:
            for k in bucket:
                if _compatible(best[k], r):
                    match = k
                    break
        if match is None:
            best[key] = r
            order.append(key)
            bucket.append(key)
        elif _richness(r) > _richness(best[match]):
            best[match] = r
    return [best[k] for k in order]


def _validator(name: str, def_name: str):
    import jsonschema

    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    # Carry $defs so internal $refs (e.g. #/$defs/photo) still resolve.
    sub = dict(schema["$defs"][def_name])
    sub["$defs"] = schema["$defs"]
    return jsonschema.Draft202012Validator(sub, format_checker=jsonschema.FormatChecker())


def _doc_validator(name: str):
    import jsonschema

    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def build(records: list[dict[str, Any]], edition_date: str,
          generated_at: str, max_event_age_days: int = 7) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    event_validator = _validator("events.schema.json", "event")
    story_validator = _validator("news.schema.json", "story")
    edition_dt = datetime.combine(datetime.fromisoformat(edition_date).date(), time.min, tzinfo=dates.EASTERN)
    cutoff = edition_dt - timedelta(days=max_event_age_days)

    events, stories = [], []
    stats = {"events_in": 0, "events_out": 0, "events_stale": 0,
             "stories_in": 0, "stories_out": 0, "content_dropped": 0}

    for record in _dedupe(records):
        if _content_dropped(record):
            stats["content_dropped"] += 1
            continue
        if record.get("kind") == "event":
            stats["events_in"] += 1
            event = to_event(record, cutoff, edition_dt)
            if event is not None and event_validator.is_valid(event):
                events.append(event)
                stats["events_out"] += 1
            elif event is None and _parse_dt(record.get("start")):
                stats["events_stale"] += 1
        elif record.get("kind") == "news":
            stats["stories_in"] += 1
            story = to_story(record)
            if story is not None and story_validator.is_valid(story):
                stories.append(story)
                stats["stories_out"] += 1

    events_doc = {
        "schemaVersion": "2.0.0",
        "generatedAt": generated_at,
        "editionDate": edition_date,
        "events": events,
    }
    news_doc = {
        "schemaVersion": "1.1.0",
        "generatedAt": generated_at,
        "editionDate": edition_date,
        "stories": stories,
    }
    return events_doc, news_doc, stats


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    p.add_argument("--out", type=Path, default=ROOT, help="directory to write into (default: repo root)")
    p.add_argument("--edition-date", default=None, help="YYYY-MM-DD (default: today Eastern)")
    p.add_argument("--max-event-age-days", type=int, default=7,
                   help="drop start-only events whose start is older than this (default 7; "
                        "events with an end are kept until that end passes)")
    p.add_argument("--dry-run", action="store_true")
    opts = p.parse_args(argv)

    records = [json.loads(l) for l in opts.records.read_text(encoding="utf-8").splitlines() if l.strip()]
    edition = opts.edition_date or dates.parse_reference(None).astimezone(dates.EASTERN).date().isoformat()
    generated = datetime.now(dates.EASTERN).isoformat(timespec="seconds")

    events_doc, news_doc, stats = build(records, edition, generated, opts.max_event_age_days)

    # Final whole-document validation before writing anything.
    ev_ok = _doc_validator("events.schema.json").is_valid(events_doc)
    nw_ok = _doc_validator("news.schema.json").is_valid(news_doc)
    print(f"records in: {len(records)}  deduped: {len(_dedupe(records))}  "
          f"content-dropped: {stats['content_dropped']}")
    print(f"events.json: {stats['events_out']}/{stats['events_in']} kept (stale {stats['events_stale']})  valid={ev_ok}")
    print(f"news.json:   {stats['stories_out']}/{stats['stories_in']} kept  valid={nw_ok}")
    if not (ev_ok and nw_ok):
        print("REFUSING to write: document failed validation")
        return 1
    if opts.dry_run:
        print("(dry run, not written)")
        return 0
    opts.out.mkdir(parents=True, exist_ok=True)
    (opts.out / "events.json").write_text(
        json.dumps(events_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (opts.out / "news.json").write_text(
        json.dumps(news_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {opts.out/'events.json'} and {opts.out/'news.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())