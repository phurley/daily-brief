"""Conservative occurrence resolution. No network or model calls.

Source snapshots are immutable; the private registry makes publication replayable.
A match must survive time, venue, recurrence and performer conflict checks.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import dates

VERSION = "occurrence-v1"
STATUSES = {"scheduled", "canceled", "postponed", "rescheduled"}
PUBLIC_FIELDS = ("occurrenceId", "canonicalUrl", "venueId", "timePrecision", "status",
                 "aliases", "legacyOccurrenceKeys", "legacySeriesKeys", "sources", "identity")
_STATUS_PREFIX = re.compile(r"^\s*[\[(]?(cancelled|canceled|postponed|rescheduled)[\])]?[\s:–—-]+", re.I)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()[:24]


def normalize(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[^\W_]+", text, re.UNICODE))


def title(record):
    return normalize(_STATUS_PREFIX.sub("", record.get("title") or ""))


def canonical_url(value):
    try:
        u = urlsplit(str(value or ""))
        if u.scheme not in ("http", "https") or not u.hostname:
            return ""
        # Preserve event IDs, dates, format and all other meaningful query keys.
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in
                 {"fbclid", "gclid", "mc_cid", "mc_eid"}]
        return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or "/",
                           urlencode(sorted(query)), ""))
    except ValueError:
        return ""


def is_detail(record):
    if record.get("urlKind"):
        return record["urlKind"] == "detail"
    u = urlsplit(record.get("canonicalUrl") or canonical_url(record.get("url")))
    return bool(re.search(r"/(?:event|events|node|e|tickets|tm-event)/[^/]+", u.path, re.I)
                or any(k.lower() in {"event_id", "eventid", "eid", "p"}
                       for k, _ in parse_qsl(u.query)))


def instant(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dates.EASTERN)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def venue(record):
    city = re.sub(r",?\s+(?:MI|Michigan)$", "", str(record.get("city") or ""), flags=re.I)
    return record.get("venueId") or normalize(record.get("venue")) + "|" + normalize(city)


def performer_signature(record):
    """Conservative formatting equivalence for advertised music lineups."""
    if not re.search(r"music|concert", record.get("category") or "", re.I):
        return None
    raw = _STATUS_PREFIX.sub("", record.get("title") or "")
    if not re.search(r",|\swith\s", raw, re.I):
        return None
    parts = re.split(r"\s+(?:with|and)\s+|\s*,\s*", raw, flags=re.I)
    names = [normalize(part) for part in parts if normalize(part)]
    return (names[0], tuple(sorted(names[1:]))) if len(names) > 1 else None


def native(record):
    if not record.get("sourceEventId") or not record.get("sourceId"):
        return None
    instance = record.get("recurrenceId")
    if not instance and record.get("recurrenceRule"):
        instance = instant(record.get("start"))
    return (record["sourceId"], record["sourceEventId"], instance or "single")


def prepare(record):
    r = dict(record)
    origin = r.get("origin") or {}
    r["sourceId"] = r.get("sourceId") or origin.get("source_slug") or urlsplit(canonical_url(r.get("url"))).netloc
    r["canonicalUrl"] = canonical_url(r.get("url"))
    r["venueId"] = venue(r)
    if r.get("sourceEventId"):
        r["seriesId"] = "series-" + digest([r["sourceId"], r["sourceEventId"]])
    r["observedAt"] = r.get("observedAt") or r.get("addedAt") or ""
    r["timePrecision"] = r.get("timePrecision") or (
        "unknown" if not r.get("start") else
        "date" if "T00:00:00" in str(r["start"]) or len(str(r["start"])) == 10 else "time")
    prefix = _STATUS_PREFIX.match(r.get("title") or "")
    if prefix:
        r["status"] = prefix[1].lower().replace("cancelled", "canceled")
        r["statusEvidence"] = prefix[0].strip()
    if r.get("status") not in STATUSES or not r.get("statusEvidence"):
        r["status"] = "scheduled"
        r.pop("statusEvidence", None)
    # Snapshot identity deliberately includes source text/fields, not just title/day.
    # Observation time and scoring are excluded so unchanged content is replay-safe.
    payload = {k: v for k, v in r.items() if k not in {
        "sourceRecordId", "id", "aliases", "addedAt", "observedAt", "score", "scoring",
        "occurrenceId", "identity", "sources", "contentFlags"}}
    r["sourceRecordId"] = r.get("sourceRecordId") or "src-" + digest(payload)
    return r


def compare(a, b, fuzzy=False):
    """Return (match/distinct/review, evidence). Unknown is never agreement."""
    na, nb = native(a), native(b)
    same_native = na is not None and na == nb
    same_url = bool(a.get("canonicalUrl") and a.get("canonicalUrl") == b.get("canonicalUrl"))
    if a.get("recurrenceId") and b.get("recurrenceId") and a["recurrenceId"] != b["recurrenceId"]:
        return "distinct", "different-recurrence"
    if (na and nb and a.get("sourceId") == b.get("sourceId") and na != nb):
        return "distinct", "different-native-id"
    if (not same_native and not same_url and is_detail(a) and is_detail(b)
        and a.get("sourceId") == b.get("sourceId")
        and urlsplit(a.get("canonicalUrl") or "").netloc == urlsplit(b.get("canonicalUrl") or "").netloc):
        return "distinct", "different-detail-id"
    va, vb = venue(a), venue(b)
    if va and vb and va != vb:
        return "distinct", "different-venue"
    performers_a = {normalize(p) for p in a.get("performers") or []}
    performers_b = {normalize(p) for p in b.get("performers") or []}
    if performers_a and performers_b and not (performers_a <= performers_b or performers_b <= performers_a):
        return "distinct", "conflicting-performers"
    ta, tb = instant(a.get("start")), instant(b.get("start"))
    known = a.get("timePrecision") == b.get("timePrecision") == "time"
    if ta != tb or not known or not ta:
        # Recurrence exceptions carry their original instance ID. For singleton
        # updates require explicit previousStart plus native identity.
        for old, new in ((a, b), (b, a)):
            if (same_native and new.get("status") == "rescheduled" and new.get("statusEvidence")
                and (instant(new.get("previousStart")) == instant(old.get("start")))
                and instant(new.get("previousStart"))):
                return "match", "native-explicit-reschedule"
        return ("review", "ambiguous-time-or-reschedule") if same_native or (same_url and title(a) == title(b)) else ("distinct", "different-or-unknown-time")
    if not a.get("venue") or not b.get("venue"):
        return "review", "unknown-venue"
    if same_native:
        return "match", "native-occurrence"
    if title(a) and title(a) == title(b):
        return "match", "exact-title-time-venue"
    if performer_signature(a) and performer_signature(a) == performer_signature(b):
        return "match", "exact-performer-list-time-venue"
    if same_url and is_detail(a) and is_detail(b):
        similarity = SequenceMatcher(None, title(a), title(b)).ratio()
        if similarity >= .92:
            return ("match" if fuzzy else "review"), "detail-url-title-typo"
    return "distinct", "insufficient-evidence"


def _rank(record):
    return ({"organizer": 3, "ticket": 2, "unknown": 1}.get(record.get("sourceAuthority"), 1),
            instant(record.get("sourceUpdatedAt")) or instant(record.get("observedAt")),
            record["sourceRecordId"])


def merge(members, occurrence_id, reasons):
    ordered = sorted(members, key=_rank, reverse=True)
    out, provenance = {}, {}
    # Core fields follow authority and recency; optional enrichment fills holes.
    for r in ordered:
        for key, value in r.items():
            if value not in (None, "", []) and key not in out:
                out[key] = value
                provenance[key] = r["sourceRecordId"]
    explicit = [r for r in ordered if r.get("statusEvidence")]
    if explicit:
        # Richness never determines status. Prefer explicit cancellation on a
        # timestamp/authority tie, while a newer explicit reinstatement can win.
        winner = max(explicit, key=lambda r: (_rank(r)[:2], r["status"] in {"canceled", "postponed"}, r["sourceRecordId"]))
        out["status"] = winner["status"]
        out["statusEvidence"] = winner["statusEvidence"]
        provenance["status"] = winner["sourceRecordId"]
    # An old performance's end is not the new end of a rescheduled event.
    if out.get("end"):
        supplier = next((r for r in ordered if r["sourceRecordId"] == provenance.get("end")), None)
        if supplier and instant(supplier.get("start")) != instant(out.get("start")):
            out.pop("end", None)
            provenance.pop("end", None)
    out["id"] = out["occurrenceId"] = occurrence_id
    out["aliases"] = sorted({str(alias) for r in members
                             for alias in [r.get("id"), *(r.get("aliases") or [])]
                             if alias and alias != occurrence_id})
    # Preserve exact pre-migration preference keys, including the source's
    # original timezone spelling. New feedback stores these aliases as well,
    # so it still applies after a rollback to legacy publication.
    out["legacyOccurrenceKeys"] = sorted({
        f"{alias}|{r.get('start') or ''}" for r in members
        for alias in [r.get("id"), *(r.get("aliases") or [])] if alias
    } | {key for r in members for key in r.get("legacyOccurrenceKeys", [])})
    out["legacySeriesKeys"] = sorted({
        str(r.get("seriesId") or f"{r.get('title') or ''}|{r.get('venue') or ''}").lower()
        for r in members
    } | {key for r in members for key in r.get("legacySeriesKeys", [])})
    out["sources"] = [{k: r[k] for k in ("sourceRecordId", "sourceId", "canonicalUrl", "observedAt",
                       "sourceUpdatedAt", "sourceAuthority", "status", "statusEvidence") if r.get(k)}
                      for r in ordered]
    conflicts = {key: sorted({str(r[key]) for r in members if r.get(key)})
                 for key in ("start", "end", "venue", "status")}
    out["identity"] = {"ruleVersion": VERSION, "reasons": sorted(set(reasons)),
                       "fields": {k: v for k, v in provenance.items() if k in
                                  {"title", "start", "end", "venue", "status", "url", "registration", "imageUrl"}},
                       "conflicts": {k: v for k, v in conflicts.items() if len(v) > 1}}
    return out


def resolve(records, registry=None, fuzzy=False):
    state = json.loads(json.dumps(registry or {"version": VERSION, "mapping": {}, "anchors": {}}))
    if state.get("version") != VERSION:
        raise ValueError("unsupported identity registry version")
    mapping, anchors = state["mapping"], state["anchors"]
    stored_reasons = state.setdefault("reasons", {})
    buckets = defaultdict(set)
    members, reasons = defaultdict(list), defaultdict(list)
    audit, reviews = [], []

    def keys(r):
        values = []
        if r.get("timePrecision") == "time" and instant(r.get("start")):
            values.append(("slot", instant(r.get("start")), venue(r)))
        if native(r):
            values.append(("native", *native(r)))
        if r.get("canonicalUrl") and is_detail(r):
            values.append(("url", r["canonicalUrl"]))
        return values

    def index(oid, r):
        for key in keys(r):
            buckets[key].add(oid)

    for oid, group in anchors.items():
        for r in group:
            index(oid, r)
    events = sorted((prepare(r) for r in records if r.get("kind") == "event"),
                    key=lambda r: (instant(r.get("observedAt")), r["sourceRecordId"]))
    for r in events:
        sid = r["sourceRecordId"]
        oid = mapping.get(sid)
        reason = stored_reasons.get(sid, "persisted-source-mapping")
        if not oid:
            candidates = set().union(*(buckets[k] for k in keys(r)))
            matches = []
            for candidate in sorted(candidates):
                evidence = [compare(a, r, fuzzy) for a in anchors[candidate]]
                positive = [why for decision, why in evidence if decision == "match"]
                # Prevent transitive fuzzy bridges and incompatible session merges.
                veto = any(decision == "distinct" and why in {
                    "different-recurrence", "different-native-id", "different-detail-id", "different-venue", "conflicting-performers"}
                           for decision, why in evidence)
                if positive and not veto:
                    matches.append((candidate, positive[0]))
                elif any(decision == "review" for decision, _ in evidence):
                    reviews.append({"sourceRecordId": sid, "candidate": candidate,
                                    "reasons": sorted({why for decision, why in evidence if decision == "review"})})
            if len(matches) == 1:
                oid, reason = matches[0]
            else:
                oid, reason = "occ-" + digest(sid), "new-occurrence"
                if len(matches) > 1:
                    reviews.append({"sourceRecordId": sid, "candidates": [o for o, _ in matches],
                                    "reasons": ["multiple-matching-occurrences"]})
            mapping[sid] = oid
            stored_reasons[sid] = reason
            # Keep identity evidence locally; source snapshots remain in the archive.
            anchor = {k: v for k, v in r.items() if k in {
                "sourceRecordId", "sourceId", "sourceEventId", "recurrenceId", "recurrenceRule",
                "canonicalUrl", "urlKind", "venueId", "venue", "city", "title", "start",
                "previousStart", "timePrecision", "status", "statusEvidence", "performers", "category"}}
            anchors.setdefault(oid, []).append(anchor)
            index(oid, anchor)
        if not any(m["sourceRecordId"] == sid for m in members[oid]):
            members[oid].append(r)
        reasons[oid].append(reason)
        audit.append({"sourceRecordId": sid, "occurrenceId": oid, "reason": reason, "ruleVersion": VERSION})
    canonical = [merge(group, oid, reasons[oid]) for oid, group in sorted(members.items())]
    report = {"ruleVersion": VERSION, "records": len(events), "occurrences": len(canonical),
              "mergedRecords": len(events) - len(canonical), "reviewCandidates": reviews,
              "conflicts": sum(bool(r["identity"]["conflicts"]) for r in canonical), "mappings": audit}
    return canonical, state, report


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
