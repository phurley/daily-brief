#!/usr/bin/env python3
"""Fallback enrichment for event records with missing required fields.

When an extracted event is missing `start` / `venue` / `city`, try:

1. a date embedded in the URL (`/2026/09/22/`, `.../2026-09-22/`, `…-260922/`);
2. fetching the event page and asking the light model for just those fields.

Bounded and best-effort: failures leave the record as-is (publish then drops it).
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from datetime import datetime, time, timezone
from typing import Any, Optional

from . import dates, llm

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_FIELDS = ("start", "end", "venue", "city", "category", "imageUrl")

TOUCHUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "start": {"type": ["string", "null"], "description": "Absolute start datetime ISO-8601 with offset, or null."},
        "end": {"type": ["string", "null"]},
        "venue": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"]},
        "imageUrl": {"type": ["string", "null"]},
    },
    "required": list(_FIELDS),
}
_SYSTEM = (
    "You fill in missing fields for one event from a web page's text. Use only "
    "information on the page. Return null for anything not stated. JSON only."
)

_URL_DATE_RES = [
    re.compile(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:/|$)"),
    re.compile(r"-(20\d{2})(\d{2})(\d{2})(?:/|$)"),   # ...-20260922/
    re.compile(r"[-/](\d{2})(\d{2})(\d{2})(?:/|$)"),    # ...-260922/ or /260922/
]

#: Pages richer in event data than the article that links to them. The article
#: often omits date/venue while the linked page states them.
_DETAIL_HOST_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*(?:"
    r"facebook\.com/events/\d+"
    r"|eventbrite\.com/e/[^\s)\]\"'<>]+"
    r"|etix\.com/[^\s)\]\"'<>]+"
    r"|ticketmaster\.com/[^\s)\]\"'<>]+"
    r"|lumatickets\.com/[^\s)\]\"'<>]+"
    r"|dice\.fm/[^\s)\]\"'<>]+"
    r"|seatgeek\.com/[^\s)\]\"'<>]+"
    r"|gametime\.co/[^\s)\]\"'<>]+"
    r"|showclix\.com/[^\s)\]\"'<>]+"
    r"|ticketweb\.com/[^\s)\]\"'<>]+"
    r")",
    re.IGNORECASE,
)
_ICAL_URL_RE = re.compile(r"https?://[^\s)\]\"'<>]*\?(?:[^\s)\]\"'<>]*&)?(?:format=ical|ical=1)", re.IGNORECASE)


def detail_links(record: dict[str, Any]) -> list[str]:
    """Ticketing / FB-event / ical links found in a candidate's own text."""
    parts = [record.get("text"), record.get("summary"), record.get("url")]
    blob = "\n".join(str(p) for p in parts if p)
    found = _DETAIL_HOST_RE.findall(blob) + _ICAL_URL_RE.findall(blob)
    out: list[str] = []
    for url in found:
        if url not in out:
            out.append(url)
    return out[:4]


def _is_ical_url(url: str) -> bool:
    return bool(_ICAL_URL_RE.fullmatch(url))


def _ical_times(url: str) -> tuple[Optional[str], Optional[str]]:
    """Fetch a small ical feed and parse DTSTART/DTEND deterministically."""
    try:
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(200_000).decode("utf-8", "replace")
    except Exception:
        return None, None

    def parse(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        value = value.strip()
        m = re.match(r"(\d{8})(?:T(\d{6})(Z)?)?$", value)
        if not m:
            return None
        date_s, time_s, utc = m.groups()
        try:
            if time_s:
                dt = datetime(int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
                              int(time_s[:2]), int(time_s[2:4]), int(time_s[4:6]),
                              tzinfo=timezone.utc if utc else dates.EASTERN)
            else:  # all-day
                dt = datetime(int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8]),
                              tzinfo=dates.EASTERN)
        except ValueError:
            return None
        return dt.isoformat()

    start = re.search(r"^DTSTART[^:]*:(.*)$", raw, re.M)
    end = re.search(r"^DTEND[^:]*:(.*)$", raw, re.M)
    return parse(start.group(1) if start else None), parse(end.group(1) if end else None)


def date_from_url(url: str) -> Optional[str]:
    for pattern in _URL_DATE_RES:
        m = pattern.search(url or "")
        if not m:
            continue
        a, b, c = m.groups()
        if len(a) == 4:
            y, mo, d = int(a), int(b), int(c)
        else:  # YY MM DD
            y, mo, d = 2000 + int(a), int(b), int(c)
        try:
            dt = datetime(y, mo, d, tzinfo=dates.EASTERN)
        except ValueError:
            continue
        return dt.isoformat()
    return None


def fetch_text(url: str, timeout: float = 20.0, limit: int = 12000) -> str:
    try:
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(400_000).decode("utf-8", "replace")
    except Exception:
        return ""
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def enrich_event(record: dict[str, Any], chat: llm.ChatClient,
                  detail_links: Optional[list[str]] = None) -> dict[str, Any]:
    """Fill missing event fields, preferring richer sources.

    Order of attack:
    1. a date embedded in the URL;
    2. an ical feed among ``detail_links`` (deterministic DTSTART parse, no LLM);
    3. the richest linked page (ticketing / FB-event / event site) for the
       touch-up model — the article page often lacks the details while the
       linked event page has them;
    4. the record's own page.
    """
    links = [u for u in (detail_links or []) if isinstance(u, str) and u.startswith("http")]
    missing = [f for f in ("start", "venue", "city", "category") if not record.get(f)]
    if not missing:
        return record
    if "start" in missing:
        for url in links:
            times = _ical_times(url)
            if times[0]:
                record["start"] = times[0]
                if times[1] and not record.get("end"):
                    record["end"] = times[1]
                missing.remove("start")
                break
    if "start" in missing:
        guess = date_from_url(record.get("url") or "")
        if guess:
            record["start"] = guess
            missing.remove("start")
    if not missing:
        return record
    text = ""
    for url in [u for u in links if not _is_ical_url(u)] + [record.get("url") or ""]:
        text = fetch_text(url)
        if text:
            break
    if not text:
        return record
    user = (
        f"EVENT: {record.get('title')}\n"
        f"Missing fields: {', '.join(missing)}\n\n"
        f"PAGE TEXT:\n{text}"
    )
    try:
        data = chat.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            TOUCHUP_SCHEMA, schema_name="event_touchup",
        )
    except llm.LLMError:
        return record
    for field in _FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip().lower() not in ("", "null", "none", "n/a"):
            record.setdefault(field, value.strip())
    return record