"""Source schedules stay private; publication materializes actual occurrences.

An end date on a series is not a performance's closing time. All expansion is
bounded, in the source timezone, and happens before occurrence reconciliation.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import dates, identity

HORIZON_DAYS = 90
MAX_OCCURRENCES = 1000
DAYS = {name: code for name, code in zip(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
    ("MO", "TU", "WE", "TH", "FR", "SA", "SU"))}
WEEKDAYS = list(DAYS.values())
DAY_RE = re.compile(r"\b(" + "|".join(DAYS) + r")s\b", re.I)
REPEATS_RE = re.compile(r"\b(?:every|each)\s+(" + "|".join(DAYS) + r")\b", re.I)
CLOCK = r"\d{1,2}(?::\d{2})?"
PERIOD = r"(?:[ap]\.?m\.?)"
TIME_RE = re.compile(r"(?<![\d/])(" + CLOCK + r")\s*(" + PERIOD + r")?\s*"
                     r"(?:(?:[-–—]|\bto\b)\s*(" + CLOCK + r")\s*)?(" + PERIOD + r")(?![A-Za-z])", re.I)


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=dates.EASTERN)
    except (ValueError, TypeError):
        return None


def ics_datetime(value, tz="America/Detroit"):
    """Compact ICS values, retaining UTC, TZID, and date-only exclusions."""
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return datetime.strptime(raw, "%Y%m%d").date().isoformat()
    if re.fullmatch(r"\d{8}T\d{6}Z?", raw):
        dt = datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=timezone.utc if raw.endswith("Z") else ZoneInfo(tz)).isoformat()
    dt = parse_dt(raw)
    return dt.isoformat() if dt else None


def jsonld_schedule(obj):
    raw = obj.get("eventSchedule")
    if not raw:
        return None
    result = {"kind": "weekly", "timezone": "America/Detroit", "rules": [],
              "excludedDates": [], "evidence": "schema.org/eventSchedule"}
    # Schedules can have different seasonal windows and multiple showtimes.
    for s in raw if isinstance(raw, list) else [raw]:
        if not isinstance(s, dict):
            return {"kind": "unknown", "evidence": "Invalid eventSchedule"}
        frequency = s.get("repeatFrequency")
        if frequency not in ("P1W", "P1D"):
            return {"kind": "unknown", "evidence": "Unsupported eventSchedule frequency"}
        tz = s.get("scheduleTimezone") or "America/Detroit"
        if result["rules"] and tz != result["timezone"]:
            return {"kind": "unknown", "evidence": "Conflicting schedule timezones"}
        result["timezone"] = tz
        byday = s.get("byDay") or (list(DAYS) if frequency == "P1D" else [])
        codes = [DAYS.get(str(d).rsplit("/", 1)[-1].lower(), str(d))
                 for d in byday if d] if isinstance(byday, list) else [DAYS.get(str(byday).rsplit("/", 1)[-1].lower(), str(byday))]
        if not codes or any(d not in WEEKDAYS for d in codes) or s.get("repeatCount"):
            return {"kind": "unknown", "evidence": "Unsupported eventSchedule rule"}
        result["rules"].append({"daysOfWeek": codes, "startTime": s.get("startTime"),
                                "endTime": s.get("endTime"), "startDate": s.get("startDate"),
                                "endDate": s.get("endDate")})
        excluded = s.get("exceptDate") or []
        result["excludedDates"].extend(excluded if isinstance(excluded, list) else [excluded])
    return result


def source_section(record, candidate):
    """Bind schedule text to this event, never a neighboring listing."""
    text = candidate.get("text") or candidate.get("content") or "\n".join(str(candidate.get(k) or "") for k in ("title", "summary", "venue"))
    url = identity.canonical_url(record.get("url"))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        links = re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", line)
        if any(identity.canonical_url(link) == url and identity.normalize(title) == identity.normalize(record.get("title"))
               for title, link in links):
            stop = next((j for j in range(i + 1, len(lines))
                         if re.match(r"\s*\*\s+\[", lines[j])), len(lines))
            return "\n".join(lines[i:stop])
    for i, line in enumerate(lines):
        if re.match(r"^\s*#{1,6}\s+", line) and identity.normalize(re.sub(r"^\s*#+\s+", "", line)) == identity.title(record):
            stop = next((j for j in range(i + 1, len(lines)) if re.match(r"\s*(?:#+\s+|\*\s+\[)", lines[j])), len(lines))
            return "\n".join(lines[i:stop])
    if identity.canonical_url(candidate.get("url")) == url and (
            identity.is_detail(record) or candidate.get("feed_kind") in ("ics", "jsonld")
            or any(identity.normalize(candidate.get(k)) == identity.title(record) for k in ("title", "page_title"))):
        return re.split(r"(?im)^#{1,6}\s+(?:Related Events|More Events|Upcoming Events|You might also like)\b", text)[0]
    return ""


def _clock(raw, period):
    hour, _, minute = raw.partition(":")
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or minute > 59:
        raise ValueError("Invalid source clock time")
    hour = hour % 12 + (12 if period.lower().startswith("p") else 0)
    return time(hour, minute).isoformat()


def _time_rules(text, days):
    result = []
    text = re.sub(r"(" + CLOCK + r"\s*[-–—]\s*" + CLOCK + r")\s*&\s*(" + CLOCK + r"\s*[-–—]\s*" + CLOCK + r")\s*(" + PERIOD + r")",
                  r"\1 \3 & \2 \3", text, flags=re.I)
    for m in TIME_RE.finditer(text):
        start, first_period, end, last_period = m.groups()
        period = first_period or last_period
        if end and not first_period and _clock(start, period) > _clock(end, last_period):
            period = "am" if last_period.lower().startswith("p") else "pm"
        result.append({"daysOfWeek": days, "startTime": _clock(start, period),
                       "endTime": _clock(end, last_period) if end else None})
    return result


def from_text(record, text):
    """Conservative parser for explicit weekday/time lines and prose.

    Listing date lines outrank promotional copy. Unsupported recurring evidence
    is retained as unknown rather than silently published as a continuous span.
    """
    if not text:
        return None
    lines = text.splitlines()
    # Metro Times puts its authoritative date line immediately after the title.
    header = lines[1].strip() if len(lines) > 1 and re.search(r"\]\(https?://", lines[0]) else ""
    weekday_text = header if DAY_RE.search(header) else text
    matches = list(DAY_RE.finditer(weekday_text))
    if not matches:
        matches = list(REPEATS_RE.finditer(weekday_text))
    if not matches:
        return None
    rules = []
    pending = []
    for i, match in enumerate(matches):
        pending.append(DAYS[match[1].lower()])
        segment = weekday_text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(weekday_text)]
        if i + 1 < len(matches) and re.fullmatch(r"\s*(?:[-–—]|through|to)\s*", segment, re.I):
            a, b = WEEKDAYS.index(pending[-1]), WEEKDAYS.index(DAYS[matches[i + 1][1].lower()])
            pending.extend(WEEKDAYS[(a + n) % 7] for n in range(1, (b - a) % 7))
        # Stop before enumerated dates, prose, or the series closing date.
        segment = re.split(r"Continues through|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.,|\n|[.!?]\s+[A-Z]", segment, maxsplit=1)[0]
        found = _time_rules(segment, pending[:])
        if found:
            rules.extend(found)
            pending = []
    if not rules:
        # Common prose: "Classes run Sundays ... 1pm–3pm".
        rules = _time_rules(weekday_text, list(dict.fromkeys(DAYS[m[1].lower()] for m in matches)))
        if rules:
            pending = []
    evidence = header or next((line for line in lines if DAY_RE.search(line) or REPEATS_RE.search(line)), "")
    if not rules and header and matches:
        rules = [{"daysOfWeek": list(dict.fromkeys(DAYS[m[1].lower()] for m in matches)), "startTime": None, "endTime": None}]
        pending = []
    if not rules or pending:
        return {"kind": "unknown", "evidence": evidence}
    start, end = parse_dt(record.get("start")), parse_dt(record.get("end"))
    existing = record.get("schedule") or {}
    schedule = {"kind": "weekly", "timezone": "America/Detroit", "rules": rules,
                "startDate": existing.get("startDate") or (start.astimezone(dates.EASTERN).date().isoformat() if start else None),
                "endDate": existing.get("endDate") or (end.astimezone(dates.EASTERN).date().isoformat() if end and start and end.date() > start.date() else None),
                "excludedDates": list(existing.get("excludedDates") or []),
                "dates": list(existing.get("dates") or []), "evidence": evidence}
    # Use source-normalized dates for the series cutoff when stated in the header.
    closing = re.search(r"Continues through\s+(.+)", header, re.I)
    if closing:
        from dateutil.parser import parse
        try:
            year = (end or start or datetime.now(dates.EASTERN)).year
            schedule["endDate"] = parse(closing[1], default=datetime(year, 1, 1), fuzzy=False).date().isoformat()
        except (ValueError, OverflowError):
            return {"kind": "unknown", "evidence": evidence}
    from dateutil.parser import parse
    year = (end or start or datetime.now(dates.EASTERN)).year
    exception_re = r"\b(?:excluding|except)\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)"
    exceptions = list(re.finditer(exception_re, text, re.I))
    for m in exceptions:
        schedule["excludedDates"].append(parse(m[1], default=datetime(year, 1, 1)).date().isoformat())
    extras = list(re.finditer(r"graduation show\s+(?:on\s+)?([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)\s+at\s+(" + CLOCK + r")\s*(" + PERIOD + r")", text, re.I))
    for m in extras:
        day = parse(m[1], default=datetime(year, 1, 1)).date()
        schedule.setdefault("dates", []).append({"start": _local(day, _clock(m[2], m[3]), dates.EASTERN).isoformat(), "end": None})
    if (re.search(r"\b(?:excluding|except)\b", text, re.I) and not exceptions
            or re.search(r"\b(?:graduation show|additional show)\b", text, re.I) and not extras):
        return {"kind": "unknown", "evidence": text}
    return schedule


def attach(record, candidate):
    supplied = record.get("schedule")
    if supplied:
        section = source_section(record, candidate) or candidate.get("text") or candidate.get("content") or ""
        if not isinstance(supplied, dict) or not supplied.get("evidence") or supplied["evidence"] not in section:
            record["schedule"] = {"kind": "unknown", "evidence": "Schedule evidence does not match this event's source"}
    if candidate.get("schedule"):
        record["schedule"] = copy.deepcopy(candidate["schedule"])
    elif not record.get("recurrenceRule"):
        try:
            found = from_text(record, source_section(record, candidate))
        except (ValueError, TypeError, OverflowError):
            found = {"kind": "unknown", "evidence": "Invalid source schedule"}
        if found and (found["kind"] != "unknown" or not record.get("schedule")):
            record["schedule"] = found
    return record


def _local(day, clock, tz):
    dt = datetime.combine(day, time.fromisoformat(clock or "00:00:00"), tzinfo=tz)
    # Do not invent an instant during the spring-forward gap.
    if dt.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != dt.replace(tzinfo=None):
        raise ValueError("Nonexistent local schedule time")
    return dt


def slots(record, first, last):
    """Yield (original instance start, actual start, end, precision)."""
    schedule = record.get("schedule") or {}
    tz = ZoneInfo(schedule.get("timezone") or record.get("recurrenceTimezone") or "America/Detroit")
    if record.get("recurrenceRule") and not record.get("recurrenceId"):
        from dateutil.rrule import rrulestr
        frequency = re.search(r"(?:^|;)FREQ=([^;]+)", record["recurrenceRule"])
        if not frequency or frequency[1] not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
            raise ValueError("Unsupported recurrence frequency")
        start = parse_dt(record.get("start"))
        if not start:
            raise ValueError("Recurrence requires a start")
        start = start.astimezone(tz)
        end = parse_dt(record.get("end"))
        duration = end.astimezone(tz) - start if end else None
        rule = rrulestr(record["recurrenceRule"], dtstart=start)
        count = 0
        for dt in rule.xafter(_local(first, "00:00:00", tz), inc=True):
            if dt.date() > last:
                break
            count += 1
            if count > MAX_OCCURRENCES:
                raise ValueError("Too many occurrences in publication window")
            yield dt, dt, dt + duration if duration is not None else None, record.get("timePrecision", "time")
        for raw in record.get("recurrenceDates") or []:
            dt = parse_dt(raw)
            if dt and first <= dt.astimezone(tz).date() <= last:
                yield dt, dt, dt + duration if duration is not None else None, record.get("timePrecision", "time")
        return
    kind = schedule.get("kind")
    if kind == "unknown":
        raise ValueError("Recurring schedule requires repair: " + str(schedule.get("evidence", ""))[:200])
    if kind == "dates":
        for slot in schedule.get("dates") or []:
            start, end = parse_dt(slot.get("start")), parse_dt(slot.get("end"))
            if not start or (slot.get("end") and not end) or (end and end < start):
                raise ValueError("Invalid explicit occurrence")
            if first <= start.astimezone(tz).date() <= last:
                yield start, start, end, "time"
        return
    if kind != "weekly" or not schedule.get("rules"):
        raise ValueError("Invalid recurring schedule")
    for rule in schedule["rules"]:
        codes = rule.get("daysOfWeek") or []
        if not codes or any(d not in WEEKDAYS for d in codes):
            raise ValueError("Invalid schedule weekdays")
        lower = rule.get("startDate") or schedule.get("startDate")
        upper = rule.get("endDate") or schedule.get("endDate")
        lower, upper = date.fromisoformat(lower) if lower else first, date.fromisoformat(upper) if upper else last
        if upper < lower:
            raise ValueError("Schedule closes before it starts")
        day = max(first, lower)
        while day <= min(last, upper):
            if WEEKDAYS[day.weekday()] in codes:
                start = _local(day, rule.get("startTime"), tz)
                end = _local(day, rule["endTime"], tz) if rule.get("endTime") else None
                if end and end < start:
                    end = _local(day + timedelta(days=1), rule["endTime"], tz)
                yield start, start, end, "time" if rule.get("startTime") else "date"
            day += timedelta(days=1)
    for slot in schedule.get("dates") or []:
        start, end = parse_dt(slot.get("start")), parse_dt(slot.get("end"))
        if not start or (slot.get("end") and not end) or (end and end < start):
            raise ValueError("Invalid additional occurrence")
        if first <= start.astimezone(tz).date() <= last:
            yield start, start, end, "time"


def is_series(record):
    return bool(record.get("recurrenceRule") and not record.get("recurrenceId")
                or record.get("schedule") and record["schedule"].get("kind") != "continuous")


def series_key(record):
    return (record.get("sourceId") or (record.get("origin") or {}).get("source_slug"),
            identity.canonical_url(record.get("url")), identity.title(record), identity.venue(record))


def expand(records, first, last):
    """Supersede old span snapshots, then materialize series before identity."""
    masters = {}
    for r in records:
        if r.get("kind") == "event" and is_series(r):
            key = series_key(r)
            rank = (r.get("sourceUpdatedAt") or r.get("observedAt") or r.get("addedAt") or "", bool(r.get("schedule")))
            if key not in masters or rank > masters[key][0]:
                masters[key] = (rank, r)
    overrides = {(r.get("sourceId"), r.get("sourceEventId"), identity.instant(r.get("recurrenceId")))
                 for r in records if r.get("recurrenceId")}
    out, failures = [], []
    for r in records:
        if r.get("kind") != "event" or series_key(r) not in masters or r.get("recurrenceId"):
            out.append(r)
        elif not is_series(r):
            start, end = parse_dt(r.get("start")), parse_dt(r.get("end"))
            if not start or not end or start.date() == end.date():
                out.append(r)  # Preserve explicit standalone performances.
    for _, r in masters.values():
        try:
            excluded = set((r.get("schedule") or {}).get("excludedDates") or []) | set(r.get("excludedDates") or [])
            excluded_instants = {identity.instant(x) for x in excluded if "T" in str(x)}
            seen, instances = set(), []
            for original, start, end, precision in slots(r, first, last):
                if original.date().isoformat() in excluded or identity.instant(original.isoformat()) in excluded_instants:
                    continue
                if (r.get("sourceId"), r.get("sourceEventId"), identity.instant(original.isoformat())) in overrides:
                    continue
                if start.isoformat() in seen:
                    continue
                seen.add(start.isoformat())
                if len(seen) > MAX_OCCURRENCES:
                    raise ValueError("Too many occurrences in publication window")
                v = copy.deepcopy(r)
                for key in ("id", "sourceRecordId", "occurrenceId", "identity", "sources", "aliases", "legacyOccurrenceKeys"):
                    v.pop(key, None)
                v["sourceEventId"] = r.get("sourceEventId") or identity.canonical_url(r.get("url"))
                v["recurrenceId"] = original.isoformat()
                v["start"], v["timePrecision"] = start.isoformat(), precision
                v.pop("end", None)
                if end:
                    v["end"] = end.isoformat()
                # A stable slot identity survives changes to the extraction text.
                v["sourceRecordId"] = "src-" + identity.digest([r.get("sourceRecordId") or r.get("id"), original.isoformat(), v])
                v = identity.prepare(v)
                v["id"] = v["sourceRecordId"]
                v["legacySeriesKeys"] = list(dict.fromkeys([*(r.get("legacySeriesKeys") or []),
                    str(r.get("title", "")).lower() + "|" + str(r.get("venue", "")).lower()]))
                instances.append(v)
            out.extend(instances)
        except (ValueError, TypeError, KeyError, OverflowError) as e:
            failures.append({"record": r, "errors": [{"field": "schedule", "reason": str(e)}]})
    return out, failures


def validate(record):
    if not is_series(record):
        return True
    try:
        start = parse_dt(record.get("start")) or parse_dt((record.get("schedule") or {}).get("startDate")) or datetime.now(dates.EASTERN)
        list(slots(record, start.date(), start.date() + timedelta(days=7)))
        return True
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
