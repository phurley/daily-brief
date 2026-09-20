#!/usr/bin/env python3
"""Collect family calendars (Google / iCloud) into calendar.json.

Sources are iCalendar (``.ics``) feeds. Google publishes them at
``https://calendar.google.com/calendar/ical/<id>/public/basic.ics`` (or a
secret ``.../private-<hash>/basic.ics`` address) and iCloud publishes them at
``webcal://pNN-caldav.icloud.com/published/2/<token>``. Those URLs work
without authentication, so no OAuth or CalDAV client is needed.

The script is intentionally dependency-free (stdlib only) so it can run from
cron or a GitHub Actions job with just ``python3``.

What it does
------------
1. Reads feed definitions from ``calendars.json`` (or the ``CALENDARS_JSON``
   environment variable for CI secrets).
2. Fetches each feed. If a URL is an HTML page instead of an ICS file, it
   discovers ``rel="alternate" type="text/calendar"`` links and ``webcal://``
   / ``*.ics`` anchors on the page and uses those unauthenticated feeds.
3. Parses VEVENTs and VTODOs, expands RRULEs into concrete occurrences over a
   rolling window, and maps each occurrence onto schemas/calendar.schema.json.
4. De-duplicates the same event coming from multiple calendars and writes
   ``calendar.json``.

Configuration lives in ``calendars.json`` (git-ignored). See
``calendars.example.json``.

    python3 scripts/update_calendar.py
    python3 scripts/update_calendar.py --dry-run --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO / "calendars.json"
DEFAULT_OUTPUT = REPO / "calendar.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "daily-brief-calendar/1.0"
)
WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
OPTIONAL_FIELDS = [
    "startTime",
    "endTime",
    "timeZone",
    "status",
    "person",
    "location",
    "description",
    "url",
    "recurrence",
]


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def _expand_env(value):
    """Replace ``${VAR}`` references so feed secrets can live in the env."""
    if not isinstance(value, str):
        return value

    def repl(match):
        name = match.group(1)
        if name not in os.environ:
            raise SystemExit(f"calendars.json references ${{{name}}} but it is not set")
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, value)


def load_config(path: Path) -> dict:
    raw = os.environ.get("CALENDARS_JSON")
    if raw:
        config = json.loads(raw)
    else:
        try:
            config = json.loads(path.read_text())
        except FileNotFoundError:
            raise SystemExit(
                f"No calendar config at {path}.\n"
                "Copy calendars.example.json to calendars.json and add your "
                "Google/iCloud ICAL URLs, or set CALENDARS_JSON."
            )
    sources = config.get("sources") or []
    if not sources:
        raise SystemExit("Calendar config has no `sources`.")
    for source in sources:
        for key, value in list(source.items()):
            if isinstance(value, str):
                source[key] = _expand_env(value)
    return config


# --------------------------------------------------------------------------
# Fetching + feed discovery
# --------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """webcal:// is iCloud/Apple's scheme for an https ICS feed."""
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://") :]
    return url


def http_get(url: str, timeout: int = 30) -> tuple[str, str]:
    request = urllib.request.Request(
        normalize_url(url),
        headers={"User-Agent": USER_AGENT, "Accept": "text/calendar, text/html;q=0.8, */*;q=0.5"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return response.headers.get_content_type(), body.decode(charset, errors="replace")


def discover_feed_urls(html: str, base_url: str) -> list[str]:
    """Find unauthenticated ICS feeds referenced by an HTML calendar page."""
    found: list[str] = []
    patterns = [
        r"<link[^>]+type=[\"']text/calendar[\"'][^>]*>",
        r"<a[^>]+href=[\"'][^\"']*(?:webcal://|\.ics(?:\?|#|[\"']))[^>]*>",
    ]
    for pattern in patterns:
        for tag in re.findall(pattern, html, flags=re.IGNORECASE):
            match = re.search(r"href=[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE)
            if match:
                found.append(urllib.parse.urljoin(base_url, match.group(1)))
    # Bare .ics / webcal references that may not be inside a link tag.
    for raw in re.findall(r"[\"'](webcal://[^\"']+|https?://[^\"']+\.ics[^\"']*)[\"']", html):
        found.append(urllib.parse.urljoin(base_url, raw))

    seen = set()
    result = []
    for url in found:
        url = normalize_url(url)
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def fetch_feed(source: dict, verbose: bool) -> list[tuple[str, str]]:
    """Return a list of ``(feed_url, ics_text)`` for one configured source."""
    url = normalize_url(source.get("url", ""))
    if not url:
        return []
    content_type, body = http_get(url)
    if "BEGIN:VCALENDAR" in body[:2000] or "text/calendar" in content_type:
        return [(url, body)]

    # Not an ICS file: treat it as a page and look for embedded feeds.
    discovered = discover_feed_urls(body, url)
    if not discovered:
        if verbose:
            print(f"  ! {source.get('name', url)}: no ICS feed found at {url}")
        return []
    feeds = []
    for feed_url in discovered:
        try:
            _, feed_body = http_get(feed_url)
        except (urllib.error.URLError, OSError) as exc:
            if verbose:
                print(f"  ! discovered feed failed {feed_url}: {exc}")
            continue
        if "BEGIN:VCALENDAR" in feed_body[:2000]:
            feeds.append((feed_url, feed_body))
    return feeds


# --------------------------------------------------------------------------
# iCalendar parsing
# --------------------------------------------------------------------------
def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def split_property(line: str):
    in_quote = False
    for index, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == ":" and not in_quote:
            head, value = line[:index], line[index + 1 :]
            break
    else:
        return None
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.upper()] = val.strip('"')
    return name, params, value


def unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append({"n": "\n", "N": "\n"}.get(nxt, nxt))
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def first(component: dict, name: str) -> str | None:
    entries = component.get(name)
    if not entries:
        return None
    return unescape(entries[0][1]).strip()


def parse_ics(text: str) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    todos: list[dict] = []
    current = None
    for line in unfold(text):
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("BEGIN:"):
            component = line.split(":", 1)[1].strip().upper()
            if component in ("VEVENT", "VTODO"):
                current = {"_type": component}
            continue
        if upper.startswith("END:"):
            component = line.split(":", 1)[1].strip().upper()
            if component == "VEVENT" and current:
                events.append(current)
                current = None
            elif component == "VTODO" and current:
                todos.append(current)
                current = None
            continue
        if current is None:
            continue
        parsed = split_property(line)
        if parsed:
            name, params, value = parsed
            current.setdefault(name, []).append((params, value))
    return events, todos


# --------------------------------------------------------------------------
# Date / recurrence handling
# --------------------------------------------------------------------------
def parse_dt(params: dict, value: str, tz: ZoneInfo):
    """Return ``('date', date)`` or ``('datetime', aware datetime in tz)``."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
        return "date", datetime.strptime(value, "%Y%m%d").date()
    if len(value) == 13:
        value += "00"
    fmt = "%Y%m%dT%H%M%S"
    if value.endswith("Z"):
        moment = datetime.strptime(value[:-1], fmt).replace(tzinfo=timezone.utc)
    elif "TZID" in params:
        try:
            moment = datetime.strptime(value, fmt).replace(tzinfo=ZoneInfo(params["TZID"]))
        except Exception:
            moment = datetime.strptime(value, fmt).replace(tzinfo=tz)
    else:
        moment = datetime.strptime(value, fmt).replace(tzinfo=tz)
    return "datetime", moment.astimezone(tz)


def as_aware(kind: str, value, tz: ZoneInfo) -> datetime:
    if kind == "date":
        return datetime.combine(value, time(0, 0), tzinfo=tz)
    return value


def parse_rrule(value: str) -> dict:
    rule = {}
    for part in value.split(";"):
        if "=" in part:
            key, val = part.split("=", 1)
            rule[key.upper()] = val.upper()
    return rule


def _last_day(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - timedelta(days=1)).day


def _add_months(year: int, month: int, count: int) -> tuple[int, int]:
    month_index = month - 1 + count
    return year + month_index // 12, month_index % 12 + 1


def _parse_byday(raw: str):
    result = []
    for token in raw.split(","):
        match = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", token.strip())
        if match:
            result.append((int(match.group(1)) if match.group(1) else None, match.group(2)))
    return result


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date | None:
    if n > 0:
        first_day = date(year, month, 1)
        offset = (weekday - first_day.weekday()) % 7
        day = 1 + offset + (n - 1) * 7
        return date(year, month, day) if day <= _last_day(year, month) else None
    last = date(year, month, _last_day(year, month))
    offset = (last.weekday() - weekday) % 7
    day = last.day - offset + (n + 1) * 7
    return date(year, month, day) if day >= 1 else None


def _occurrence_dates(start_date: date, rule: dict):
    """Yield occurrence dates (no time) for a supported subset of RRULE."""
    freq = rule.get("FREQ", "")
    interval = int(rule.get("INTERVAL", "1") or 1)
    byday = _parse_byday(rule["BYDAY"]) if rule.get("BYDAY") else []
    bymonthday = [int(x) for x in rule["BYMONTHDAY"].split(",")] if rule.get("BYMONTHDAY") else []
    bymonth = [int(x) for x in rule["BYMONTH"].split(",")] if rule.get("BYMONTH") else []
    guard = 0

    def bounded():
        nonlocal guard
        guard += 1
        if guard > 200000:
            raise StopIteration

    if freq == "DAILY":
        current = start_date
        while True:
            bounded()
            if (not byday or WEEKDAY_CODES[current.weekday()] in [c for _, c in byday]) and (
                not bymonthday or current.day in bymonthday
            ) and (not bymonth or current.month in bymonth):
                yield current
            current += timedelta(days=interval)

    elif freq == "WEEKLY":
        if byday:
            anchor = start_date - timedelta(days=start_date.weekday())
            week = anchor
            while True:
                bounded()
                for _ordinal, code in sorted(byday, key=lambda item: WEEKDAY_CODES.index(item[1])):
                    occurrence = week + timedelta(days=WEEKDAY_CODES.index(code))
                    if occurrence >= start_date:
                        yield occurrence
                week += timedelta(weeks=interval)
        else:
            current = start_date
            while True:
                bounded()
                yield current
                current += timedelta(weeks=interval)

    elif freq == "MONTHLY":
        year, month = start_date.year, start_date.month
        while True:
            bounded()
            days: list[int] = []
            last = _last_day(year, month)
            if bymonthday:
                for value in bymonthday:
                    if 0 < value <= last:
                        days.append(value)
                    elif value < 0:
                        shifted = last + value + 1
                        if 1 <= shifted <= last:
                            days.append(shifted)
            elif byday:
                for ordinal, code in byday:
                    found = _nth_weekday(year, month, WEEKDAY_CODES.index(code), ordinal or 1)
                    if found:
                        days.append(found.day)
            else:
                days = [start_date.day]
            for day in sorted(set(days)):
                occurrence = date(year, month, day)
                if occurrence >= start_date:
                    yield occurrence
            year, month = _add_months(year, month, interval)

    elif freq == "YEARLY":
        year = start_date.year
        while True:
            bounded()
            months = bymonth or [start_date.month]
            for month in months:
                if not 1 <= month <= 12:
                    continue
                last = _last_day(year, month)
                days: list[int] = []
                if bymonthday:
                    for value in bymonthday:
                        if 0 < value <= last:
                            days.append(value)
                        elif value < 0:
                            shifted = last + value + 1
                            if 1 <= shifted <= last:
                                days.append(shifted)
                elif byday:
                    for ordinal, code in byday:
                        found = _nth_weekday(year, month, WEEKDAY_CODES.index(code), ordinal or 1)
                        if found:
                            days.append(found.day)
                else:
                    days = [start_date.day]
                for day in sorted(set(days)):
                    if 1 <= day <= last:
                        occurrence = date(year, month, day)
                        if occurrence >= start_date:
                            yield occurrence
            year += interval
    else:
        yield start_date


def _parse_until(value: str, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if len(value) == 8 and value.isdigit():
        return datetime.combine(datetime.strptime(value, "%Y%m%d").date(), time(23, 59, 59), tzinfo=tz)
    if len(value) == 13:
        value += "00"
    fmt = "%Y%m%dT%H%M%S"
    if value.endswith("Z"):
        moment = datetime.strptime(value[:-1], fmt).replace(tzinfo=timezone.utc)
    else:
        moment = datetime.strptime(value, fmt).replace(tzinfo=tz)
    return moment.astimezone(tz)


def expand_occurrences(
    dtstart: datetime,
    rule: dict | None,
    exdates: set[datetime],
    rdates: list[datetime],
    window_start: date,
    window_end: date,
) -> list[datetime]:
    occurrences: list[datetime] = []
    if rule:
        count = int(rule["COUNT"]) if rule.get("COUNT") else None
        until = _parse_until(rule.get("UNTIL", ""), dtstart.tzinfo)
        emitted = 0
        for occurrence_date in _occurrence_dates(dtstart.date(), rule):
            occurrence = datetime.combine(occurrence_date, dtstart.timetz())
            if count is not None and emitted >= count:
                break
            emitted += 1
            if until and occurrence > until:
                break
            if occurrence.date() > window_end:
                break
            if occurrence.date() < window_start:
                continue
            occurrences.append(occurrence)
    elif window_start <= dtstart.date() <= window_end:
        occurrences.append(dtstart)

    occurrences.extend(r for r in rdates if window_start <= r.date() <= window_end)
    occurrences = sorted({occ for occ in occurrences if occ not in exdates})
    return occurrences


# --------------------------------------------------------------------------
# Mapping iCalendar -> schema item
# --------------------------------------------------------------------------
def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "event"


def normalize_http_url(value: str | None, fallback: str | None) -> str | None:
    for candidate in (value, fallback):
        if not candidate:
            continue
        candidate = normalize_url(candidate)
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def infer_type(title: str, categories: str, is_todo: bool) -> str:
    if is_todo:
        return "task"
    text = f"{title} {categories}".lower()
    if "birthday" in text:
        return "birthday"
    if "anniversary" in text:
        return "anniversary"
    if "holiday" in text:
        return "holiday"
    if "reminder" in text:
        return "reminder"
    if "appointment" in text or re.search(r"\bappt\b", text):
        return "appointment"
    if "task" in text or "to-do" in text or "todo" in text:
        return "task"
    return "event"


def map_status(value: str | None) -> str | None:
    if not value:
        return None
    return {
        "CONFIRMED": "confirmed",
        "TENTATIVE": "tentative",
        "CANCELLED": "cancelled",
    }.get(value.upper())


def make_id(title: str, when: date, start_time: str | None, uid: str | None) -> str:
    base = f"{slugify(title)}-{when.isoformat()}"
    if start_time:
        base += f"-{start_time.replace(':', '')}"
    if uid:
        base += f"-{hashlib.sha1(uid.encode()).hexdigest()[:6]}"
    return base


def clean_recurrence(rule: dict, dtstart: date) -> dict | None:
    """Map an RRULE onto the schema's recurrence shape, or return None.

    app.js expands recurrence as daily/interval, weekly + daysOfWeek, monthly
    on the start day-of-month, and yearly on the start month/day. Anything
    outside that subset is expanded server-side instead, because the schema
    cannot represent it faithfully.
    """
    freq = rule.get("FREQ")
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        return None
    unsupported = {"BYSETPOS", "BYYEARDAY", "BYWEEKNO", "BYHOUR", "BYMINUTE", "BYSECOND", "WKST"}
    if unsupported & set(rule):
        return None

    recurrence: dict = {"frequency": freq.lower()}
    interval = int(rule.get("INTERVAL", "1") or 1)
    if interval > 1:
        recurrence["interval"] = interval

    if freq == "WEEKLY":
        if rule.get("BYMONTHDAY") or rule.get("BYMONTH"):
            return None
        if rule.get("BYDAY"):
            days = [code for _ordinal, code in _parse_byday(rule["BYDAY"])]
            if not days:
                return None
            recurrence["daysOfWeek"] = sorted(set(days), key=WEEKDAY_CODES.index)
    elif freq == "MONTHLY":
        if rule.get("BYDAY") or rule.get("BYMONTH"):
            return None
        if rule.get("BYMONTHDAY"):
            if [int(x) for x in rule["BYMONTHDAY"].split(",")] != [dtstart.day]:
                return None
    elif freq == "YEARLY":
        if rule.get("BYDAY"):
            return None
        if rule.get("BYMONTHDAY") and [int(x) for x in rule["BYMONTHDAY"].split(",")] != [dtstart.day]:
            return None
        if rule.get("BYMONTH") and [int(x) for x in rule["BYMONTH"].split(",")] != [dtstart.month]:
            return None
    elif freq == "DAILY":
        if rule.get("BYDAY") or rule.get("BYMONTHDAY") or rule.get("BYMONTH"):
            return None
    return recurrence


def compose_item(
    title: str,
    item_type: str,
    when: date,
    start_time: str | None,
    end_time: str | None,
    status: str | None,
    person: str | None,
    location: str | None,
    description: str | None,
    link: str | None,
    uid: str | None,
) -> dict:
    item = {
        "id": make_id(title, when, start_time, uid),
        "type": item_type,
        "title": title,
        "date": when.isoformat(),
    }
    if start_time:
        item["startTime"] = start_time
    if end_time:
        item["endTime"] = end_time
    if status:
        item["status"] = status
    if person:
        item["person"] = person
    if location:
        item["location"] = location
    if description:
        item["description"] = description
    if link:
        item["url"] = link
    item["_uid"] = uid
    return item


def build_items(components: list[dict], source: dict, tz: ZoneInfo, window_start: date, window_end: date):
    person = source.get("person")
    source_link = normalize_http_url(source.get("link"), source.get("url"))
    items = []
    for component in components:
        is_todo = component.get("_type") == "VTODO"
        start_entry = component.get("DTSTART") or component.get("DUE") or component.get("DTSTAMP")
        if not start_entry:
            continue
        params, value = start_entry[0]
        kind, start_value = parse_dt(params, value, tz)
        all_day = kind == "date"
        dtstart_value = as_aware(kind, start_value, tz)

        status = map_status(first(component, "STATUS"))
        if status == "cancelled":
            continue

        end_dt = None
        if component.get("DTEND"):
            end_params, end_value = component["DTEND"][0]
            end_kind, parsed_end = parse_dt(end_params, end_value, tz)
            end_dt = as_aware(end_kind, parsed_end, tz)
        elif component.get("DURATION"):
            match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", first(component, "DURATION") or "")
            if match:
                end_dt = dtstart_value + timedelta(
                    days=int(match.group(1) or 0),
                    hours=int(match.group(2) or 0),
                    minutes=int(match.group(3) or 0),
                )

        rule = parse_rrule(first(component, "RRULE")) if first(component, "RRULE") else None
        exdates = set()
        for params, value in component.get("EXDATE", []):
            for chunk in value.split(","):
                parsed_kind, parsed = parse_dt(params, chunk, tz)
                exdates.add(as_aware(parsed_kind, parsed, tz))
        rdates = []
        for params, value in component.get("RDATE", []):
            for chunk in value.split(","):
                parsed_kind, parsed = parse_dt(params, chunk, tz)
                rdates.append(as_aware(parsed_kind, parsed, tz))

        title = first(component, "SUMMARY") or "(untitled)"
        categories = first(component, "CATEGORIES") or ""
        location = first(component, "LOCATION") or None
        description = first(component, "DESCRIPTION") or None
        event_url = first(component, "URL")
        uid = first(component, "UID")
        item_type = infer_type(title, categories, is_todo)
        link = normalize_http_url(event_url, source_link)
        base_start = None if all_day else dtstart_value.strftime("%H:%M")
        base_end = None
        if end_dt and not all_day and end_dt.date() == dtstart_value.date() and end_dt > dtstart_value:
            base_end = end_dt.strftime("%H:%M")

        # A recurring series with no exceptions/deviations can be stored once
        # and expanded by app.js, which keeps calendar.json small.
        recurrence = None
        if rule and not exdates and not rdates and rule.get("FREQ") in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
            recurrence = clean_recurrence(rule, dtstart_value.date())
            if recurrence is not None:
                until_date = None
                if rule.get("UNTIL"):
                    until_date = _parse_until(rule["UNTIL"], tz).date()
                elif rule.get("COUNT"):
                    limit = int(rule["COUNT"])
                    found = None
                    for index, occurrence_date in enumerate(_occurrence_dates(dtstart_value.date(), rule), start=1):
                        found = occurrence_date
                        if index >= limit:
                            break
                    if found is None:
                        recurrence = None
                    else:
                        until_date = found
                if recurrence is not None:
                    if (until_date and until_date < window_start) or dtstart_value.date() > window_end:
                        recurrence = None
                    else:
                        if until_date:
                            recurrence["until"] = until_date.isoformat()
                        item = compose_item(
                            title, item_type, dtstart_value.date(), base_start, base_end,
                            status, person, location, description, link, uid,
                        )
                        item["recurrence"] = recurrence
                        items.append(item)
                        continue

        for occurrence in expand_occurrences(dtstart_value, rule, exdates, rdates, window_start, window_end):
            start_time = None if all_day else occurrence.strftime("%H:%M")
            end_time = None
            if end_dt and not all_day:
                occurrence_end = end_dt + (occurrence - dtstart_value)
                if occurrence_end.date() == occurrence.date() and occurrence_end > occurrence:
                    end_time = occurrence_end.strftime("%H:%M")
            items.append(
                compose_item(
                    title, item_type, occurrence.date(), start_time, end_time,
                    status, person, location, description, link, uid,
                )
            )
    return items


# --------------------------------------------------------------------------
# De-duplication
# --------------------------------------------------------------------------
def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower())


def richness(item: dict) -> int:
    return sum(1 for field in OPTIONAL_FIELDS if item.get(field))


def merge(group: list[dict]) -> dict:
    group = sorted(group, key=richness, reverse=True)
    base = group[0]
    for other in group[1:]:
        for field in OPTIONAL_FIELDS:
            if not base.get(field) and other.get(field):
                base[field] = other[field]
        people = [p for p in {base.get("person"), other.get("person")} if p]
        if people:
            base["person"] = ", ".join(sorted(people))
    if not base.get("_uid"):
        base["_uid"] = next((item.get("_uid") for item in group if item.get("_uid")), None)
    return base


def dedupe(items: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for item in items:
        signature = (title_key(item["title"]), item["date"], item.get("startTime", ""))
        groups.setdefault(signature, []).append(item)

    merged = [merge(group) for group in groups.values()]

    # Collapse the same occurrence showing up through two subscribed calendars.
    # Key by UID *and* the occurrence so series instances are not merged.
    by_uid: dict[tuple, list[dict]] = {}
    unkeyed = []
    for item in merged:
        if item.get("_uid"):
            key = (item["_uid"], item["date"], item.get("startTime", ""))
            by_uid.setdefault(key, []).append(item)
        else:
            unkeyed.append(item)
    collapsed = [merge(group) for group in by_uid.values()]

    result = unkeyed + collapsed
    result.sort(key=lambda item: (item["date"], item.get("startTime", "99:99"), item["title"].lower()))
    return result


def unique_ids(items: list[dict]) -> None:
    used: set[str] = set()
    for item in items:
        base = item["id"]
        candidate = base
        counter = 2
        while candidate in used:
            candidate = f"{base}-{counter}"
            counter += 1
        item["id"] = candidate
        used.add(candidate)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def output_path(config: dict, override: str | None) -> Path:
    if override:
        return Path(override)
    configured = config.get("output")
    return REPO / configured if configured else DEFAULT_OUTPUT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true", help="print a summary without writing calendar.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    tz_name = config.get("timeZone", "America/Detroit")
    tz = ZoneInfo(tz_name)
    today = datetime.now(tz).date()
    window_start = today - timedelta(days=int(config.get("historyDays", 14)))
    window_end = today + timedelta(days=int(config.get("horizonDays", 550)))

    all_items: list[dict] = []
    for source in config["sources"]:
        if source.get("enabled") is False:
            continue
        name = source.get("name", source.get("url", "?"))
        try:
            feeds = fetch_feed(source, args.verbose)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"!! {name}: fetch failed ({exc})", file=sys.stderr)
            continue
        source_count = 0
        for _feed_url, ics_text in feeds:
            events, todos = parse_ics(ics_text)
            items = build_items(events + todos, source, tz, window_start, window_end)
            all_items.extend(items)
            source_count += len(items)
        if args.verbose:
            print(f"  {name}: {source_count} occurrences from {len(feeds)} feed(s)")

    items = dedupe(all_items)
    unique_ids(items)
    for item in items:
        item.pop("_uid", None)

    document = {
        "schemaVersion": "1.0.0",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "timeZone": tz_name,
        "items": items,
    }

    out = output_path(config, args.output)
    if args.dry_run:
        print(f"{len(all_items)} raw -> {len(items)} de-duplicated items; would write {out}")
        return 0
    out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {out} ({len(items)} items)")
    return 0


if __name__ == "__main__":
    sys.exit(main())