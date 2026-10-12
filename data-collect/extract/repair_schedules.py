"""Repair legacy continuous spans from their saved source candidates, offline.

Preview by default; --apply backs up the store, preserves source snapshots, and
atomically saves corrected schedules. No model calls or extractor-version reset.
"""
import argparse
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import identity, schedules


def repair(records, candidates):
    by_origin = {}
    by_url = {}
    for c in candidates:
        key = (c.get("source_slug"), c.get("chunk_id") or c.get("item_id"))
        by_origin[key] = c
        links = re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", c.get("text") or "") + [c.get("url")]
        for url in set(links):
            by_url.setdefault((c.get("source_slug"), identity.canonical_url(url)), []).append(c)
    repaired, unresolved, out = [], [], []
    for record in records:
        r = copy.deepcopy(record)
        start, end = schedules.parse_dt(r.get("start")), schedules.parse_dt(r.get("end"))
        if r.get("kind") == "event" and not r.get("schedule") and not r.get("recurrenceRule") and start and end and end.date() > start.date():
            origin = r.get("origin") or {}
            c = by_origin.get((origin.get("source_slug"), origin.get("chunk_id")))
            if c:
                schedules.attach(r, c)
            if not r.get("schedule"):
                for c in reversed(by_url.get((origin.get("source_slug"), identity.canonical_url(r.get("url"))), [])):
                    schedules.attach(r, c)
                    if r.get("schedule"):
                        break
            if r.get("schedule"):
                old_id = r.get("sourceRecordId") or r.get("id")
                r.pop("sourceRecordId", None)
                r["supersedesSourceRecordId"] = old_id
                r = identity.prepare(r)
                r["id"] = r["sourceRecordId"]
                detail = {"title": r.get("title"), "url": r.get("url"), "schedule": r["schedule"], "supersedes": old_id}
                (unresolved if r["schedule"]["kind"] == "unknown" else repaired).append(detail)
        out.append(r)
    return out, {"repaired": repaired, "unresolved": unresolved}


def main(argv=None):
    default = Path(__file__).resolve().parent.parent / "processed"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=default / "extracted_records.jsonl")
    parser.add_argument("--candidates", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    opts = parser.parse_args(argv)
    records = [json.loads(line) for line in opts.records.read_text().splitlines() if line.strip()]
    wanted = {(r.get("origin", {}).get("source_slug"), r.get("origin", {}).get("chunk_id"))
              for r in records if r.get("kind") == "event" and not r.get("schedule")}
    wanted_urls = {identity.canonical_url(r.get("url")) for r in records
                   if r.get("kind") == "event" and not r.get("schedule") and r.get("end")}
    candidates = []
    archive = opts.candidates or opts.records.with_name("source_candidate_archive.jsonl")
    if archive.exists():
        with archive.open() as stream:
            for line in stream:
                row = json.loads(line)
                c = row.get("candidate") or row
                if (c.get("source_slug"), c.get("chunk_id") or c.get("item_id")) in wanted:
                    candidates.append(c)
    # Current chunks recover candidates collected before the archive existed.
    if not opts.candidates:
        for path in sorted(opts.records.parent.glob("*/chunks.jsonl")):
            with path.open() as stream:
                for line in stream:
                    c = json.loads(line)
                    links = re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", c.get("text") or "") + [c.get("url")]
                    if (c.get("source_slug"), c.get("chunk_id")) in wanted or any(identity.canonical_url(u) in wanted_urls for u in links):
                        candidates.append(c)
    updated, report = repair(records, candidates)
    report["generatedAt"] = datetime.now(timezone.utc).isoformat()
    print(f"schedules: repaired {len(report['repaired'])}, unresolved {len(report['unresolved'])}")
    if opts.report:
        identity.atomic_json(opts.report, report)
    if opts.apply and updated != records:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = opts.records.with_name("extracted_records.before-schedules-" + stamp + ".jsonl")
        backup.write_bytes(opts.records.read_bytes())
        old_ids = {r.get("sourceRecordId") or r.get("id") for r in records}
        with opts.records.with_name("event_source_archive.jsonl").open("a") as stream:
            for r in updated:
                if (r.get("sourceRecordId") or r.get("id")) not in old_ids:
                    stream.write(json.dumps(r, ensure_ascii=False) + "\n")
        temporary = opts.records.with_suffix(".jsonl.tmp")
        temporary.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in updated))
        temporary.replace(opts.records)
        print("saved repaired store; backup:", backup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
