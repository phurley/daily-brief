"""Recheck imminent/recommended organizer detail pages with a bounded HTTP budget.

No model calls, no inferred cancellations from missing pages. Failed requests
retain the last facts and never advance the successful-check timestamp.
"""
import argparse
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from extract import evidence, identity, pipeline, schedules
from verified_calendars import fetch


def due(records, specs, state, now, favorites=()):
    work = {}
    for r in records:
        spec = specs.get(r.get("sourceId") or r.get("origin", {}).get("source_slug"), {})
        if r.get("kind") != "event" or not spec.get("verified_calendar") or schedules.is_series(r):
            continue
        start = schedules.parse_dt(r.get("start"))
        if not start or start < now - timedelta(days=1) or start > now + timedelta(days=90):
            continue
        url = identity.canonical_url(r.get("url"))
        if not url or not identity.is_detail(r):
            continue
        days = (start - now).total_seconds() / 86400
        recommended = r.get("id") in favorites or url in favorites
        hours = 3 if days <= 1 else 12 if days <= 3 or recommended else 24 if days <= 7 else 168
        checked = schedules.parse_dt(state.get(url, {}).get("lastSuccess"))
        retry_at = schedules.parse_dt(state.get(url, {}).get("retryAt"))
        if retry_at and retry_at > now:
            continue
        if not checked or now - checked >= timedelta(hours=hours):
            work[url] = min(work.get(url, (999, float("inf"), url)), (0 if recommended else 1, days, url))
    return [u for u, _ in sorted(work.items(), key=lambda row: row[1])]


def refresh(records, urls, now, state, getter=fetch):
    updated = copy.deepcopy(records)
    changes, failures = [], []
    for url in urls:
        try:
            soup = BeautifulSoup(getter(url), "html.parser")
            structured = []
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    structured.extend(evidence.event_objects(json.loads(script.string or script.get_text())))
                except (ValueError,TypeError):
                    pass
            for node in soup.select("nav,header,footer,script,style,.tribe-events-related-events"):
                node.decompose()
            body = soup.get_text(" ", strip=True)
            if not body or "captcha" in body[:1000].lower():
                raise ValueError("Empty or blocked event page")
            # Only bind announcements to a title actually present on this page.
            matching = [i for i, r in enumerate(updated) if identity.canonical_url(r.get("url")) == url
                        and identity.title(r) in identity.normalize(body)]
            if not matching:
                raise ValueError("Event title not found on detail page")
            for index in matching:
                r = updated[index]
                before = dict(r)
                for obj in structured:
                    if identity.normalize(obj.get('name')) != identity.title(r):
                        continue
                    obj_start = schedules.parse_dt(obj.get('startDate'))
                    if obj_start and obj_start != schedules.parse_dt(r.get('start')):
                        continue
                    r.update(evidence.offer_fields(obj.get('offers')))
                    if obj.get('typicalAgeRange'):
                        r['ageRestriction'] = obj['typicalAgeRange']
                evidence.apply(r, body, checked_at=now.isoformat(timespec="seconds"))
                replacement = schedules.parse_dt(r.get('replacementStart'))
                if replacement and now - timedelta(days=1) <= replacement <= now + timedelta(days=90):
                    moved = dict(r, start=replacement.isoformat(), previousStart=r['start'],status='rescheduled')
                    for key in ('id','sourceRecordId','end','replacementStart'):
                        moved.pop(key,None)
                    moved = identity.prepare(moved)
                    moved['id'] = moved['sourceRecordId']
                    if not any(x.get('sourceRecordId') == moved['sourceRecordId'] for x in updated):
                        updated.append(moved)
                # Read explicit admission from known detail labels, not arbitrary
                # dollar amounts in biographies, fundraisers or related cards.
                price = soup.select_one('.tribe-events-event-cost, .tribe-events-cost, [itemprop="price"]')
                if price:
                    r.update(evidence.price_fields(price.get_text(" ", strip=True)))
                if any(r.get(k) != before.get(k) for k in ("status", "statusEvidence", "price", "priceMin", "priceMax", "availability", "start", "end")):
                    old_id = r.get("sourceRecordId") or r.get("id")
                    r.pop("sourceRecordId", None)
                    r = identity.prepare(r)
                    r["id"] = r["sourceRecordId"]
                    changes.append({"url": url, "title": r["title"], "previousSourceRecordId": old_id,
                                    "status": r.get("status"), "price": r.get("price")})
                    updated[index] = r
            state[url] = {"lastSuccess": now.isoformat(timespec="seconds")}
        except Exception as exc:
            failures.append({"url": url, "error": str(exc)})
            state[url] = {**state.get(url, {}), "retryAt": (now + timedelta(hours=3)).isoformat(timespec="seconds"), "error": str(exc)}
    return updated, {"checkedAt": now.isoformat(timespec="seconds"), "checked": len(urls), "changes": changes, "failures": failures}


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=root / "processed/extracted_records.jsonl")
    parser.add_argument("--sources", type=Path, default=root / "source.json")
    parser.add_argument("--recommendations", type=Path, default=root.parent / "recommendations.json")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument('--timeout',type=float,default=10,help='HTTP timeout per attempt in seconds')
    parser.add_argument("--apply", action="store_true", help="fetch due pages and save verified changes")
    opts = parser.parse_args()
    if opts.limit < 0 or opts.timeout <= 0:
        parser.error("limit must be nonnegative and timeout positive")
    if not opts.records.exists():
        return
    records = pipeline.load_store(opts.records)
    state_path = opts.records.with_name("detail_refresh_state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    recommendations = json.loads(opts.recommendations.read_text()) if opts.recommendations.exists() else {}
    favorites = {e.get("url") for d in recommendations.get("days", []) for e in d.get("events", [])}
    now = datetime.now(ZoneInfo("America/Detroit"))
    urls = due(records, pipeline.load_source_specs(opts.sources), state, now, favorites)[:opts.limit]
    if not opts.apply:
        print(f"{len(urls)} detail pages due (preview; no network calls)")
        return
    updated, report = refresh(records, urls, now, state, getter=lambda url: fetch(url,timeout=opts.timeout))
    archive = opts.records.with_name("event_source_archive.jsonl")
    known = {r.get("sourceRecordId") for r in pipeline.load_store(archive)}
    with archive.open("a") as stream:
        for r in [*records, *updated]:
            if r.get("kind") == "event":
                r = identity.prepare(r)
            if r.get("kind") == "event" and r.get("sourceRecordId") not in known:
                stream.write(json.dumps(r, ensure_ascii=False) + "\n")
                known.add(r.get("sourceRecordId"))
    temp = opts.records.with_suffix(".jsonl.tmp")
    temp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in updated) + "\n")
    temp.replace(opts.records)
    identity.atomic_json(state_path, state)
    identity.atomic_json(opts.records.with_name("detail_refresh_report.json"), report)
    print(f"detail refresh: {report['checked']} checked, {len(report['changes'])} changes, {len(report['failures'])} failed")


if __name__ == "__main__":
    main()
