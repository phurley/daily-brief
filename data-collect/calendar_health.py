"""Offline end-to-end coverage audit; private operational evidence, not a quota."""
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from extract.funnel import slugify
from extract.identity import atomic_json
from extract.retry import load_states


def read(path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def build(catalog, crawl, processed, events):
    states = load_states(processed / "extraction_index.jsonl")
    extracted = rows(processed / "extracted_records.jsonl")
    quarantine = read(processed / "event_quarantine.json", {}).get("records", [])
    published = read(events, {}).get("events", [])
    report = {"generatedAt": datetime.now(ZoneInfo("America/Detroit")).isoformat(timespec="seconds"),
              "sources": [], "notes": ["Counts distinguish current candidates from retained historical snapshots.",
                                         "Sparse dates are not evidence that a calendar is fully covered."]}
    for source in catalog["sources"]:
        slug = slugify(source["name"])
        attempt = read(crawl / slug / "calendar_attempt.json", {})
        meta = read(crawl / slug / "meta.json", {}).get("crawl", {})
        current = rows(processed / slug / "feed_items.jsonl") + rows(processed / slug / "chunks.jsonl")
        dropped = rows(processed / slug / "feed_items_dropped.jsonl") + rows(processed / slug / "chunks_dropped.jsonl")
        outcomes = Counter(s.get("outcome", "unknown") for fp, s in states.items() if fp.startswith(slug + ":"))
        source_events = [e for e in published if any(s.get("sourceId") == slug for s in e.get("sources", []))]
        report["sources"].append({"name": source["name"], "slug": slug,
            "verified": bool(source.get("verified_calendar")), "attempt": attempt or None,
            "lastSuccessfulFetch": meta.get("fetched_at"),
            "currentCandidates": len(current), "currentDuplicates": sum(not r.get("is_canonical", True) for r in current),
            "candidateExclusions": {"outOfArea": sum(bool(r.get('signals',{}).get('locality',{}).get('out_of_area')) for r in current)},
            "funnelDropped": dict(Counter(r.get("filter_reason", "unknown") for r in dropped)),
            "retainedOutcomes": dict(outcomes), "pendingOrFailed": sum(outcomes[k] for k in ("retry", "exhausted")),
            "retainedSourceSnapshots": sum((r.get("sourceId") or r.get("origin", {}).get("source_slug")) == slug for r in extracted),
            "quarantined": sum((q.get("record", {}).get("sourceId") or q.get("record", {}).get("origin", {}).get("source_slug")) == slug for q in quarantine),
            "publishedOccurrences": len(source_events), "publishedByDate": dict(sorted(Counter(e["start"][:10] for e in source_events).items()))})
    return report


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=root / "source.json")
    parser.add_argument("--crawl", type=Path, default=root / "crawl")
    parser.add_argument("--processed", type=Path, default=root / "processed")
    parser.add_argument("--events", type=Path, default=root.parent / "events.json")
    parser.add_argument("--out", type=Path, default=root / "processed/calendar_health.json")
    parser.add_argument("--date", help="inspect one YYYY-MM-DD")
    opts = parser.parse_args()
    report = build(read(opts.sources, {}), opts.crawl, opts.processed, opts.events)
    atomic_json(opts.out, report)
    for s in report["sources"]:
        if s["verified"]:
            attempt = s["attempt"] or {}
            count = s["publishedByDate"].get(opts.date, 0) if opts.date else s["publishedOccurrences"]
            print(f"{s['name']}: {attempt.get('status', 'never checked')}; {count} published; {s['pendingOrFailed']} pending/failed; {s['quarantined']} quarantined")


if __name__ == "__main__":
    main()
