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
import threading
import urllib.request
from datetime import datetime, time, timezone
from pathlib import Path
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


def fetch_html(url: str, timeout: float = 20.0, limit: int = 400_000) -> str:
    """Fetch a page's raw HTML (bounded); callers derive text/JSON-LD from it."""
    try:
        request = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(limit).decode("utf-8", "replace")
    except Exception:
        return ""


def _html_to_text(raw: str, limit: int = 12000) -> str:
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def fetch_text(url: str, timeout: float = 20.0, limit: int = 12000) -> str:
    return _html_to_text(fetch_html(url, timeout=timeout), limit=limit)


_JSONLD_BLOCK_RE = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I
)


def _venue_city_from_place(place: Any) -> tuple[str, str]:
    """(venue, city) from one schema.org Place / string."""
    if isinstance(place, str):
        city = re.search(r"([A-Za-z .]+),\s*[A-Z]{2}\b", place)
        return place.strip(), (city.group(1).strip() if city else "")
    if isinstance(place, dict):
        name = str(place.get("name") or "").strip()
        addr = place.get("address")
        if isinstance(addr, dict):
            return name, str(addr.get("addressLocality") or "").strip()
        if isinstance(addr, str):
            city = re.search(r"([A-Za-z .]+),\s*[A-Z]{2}\b", addr)
            return name, (city.group(1).strip() if city else "")
        return name, ""
    return "", ""


def _jsonld_venue_city(location: Any) -> tuple[str, str]:
    """(venue, city) from a schema.org location (string/Place/list).

    Lists may mix ``VirtualLocation`` (a link, no name) with the actual
    ``Place`` — prefer entries that carry a name.
    """
    if isinstance(location, list):
        named = [item for item in location
                 if isinstance(item, dict) and item.get("name")]
        if named:
            return _venue_city_from_place(named[0])
        return _venue_city_from_place(location[0]) if location else ("", "")
    return _venue_city_from_place(location)


def _jsonld_event_fields(raw_html: str) -> dict[str, Any]:
    """Deterministic event fields from JSON-LD ``Event`` blocks in a page.

    Detail pages that carry schema.org Event markup (ticketing sites, venue
    calendars, university event systems) state exactly the fields whose
    absence fails extraction — parsed here with no LLM call.
    """
    for match in _JSONLD_BLOCK_RE.finditer(raw_html):
        try:
            data = json.loads(match.group(1).strip())
        except Exception:  # noqa: BLE001 - tolerate malformed JSON-LD
            continue
        objs: list[Any] = list(data) if isinstance(data, list) else [data]
        graph: list[Any] = []
        for obj in objs:
            if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
                graph.extend(obj["@graph"])
            else:
                graph.append(obj)
        for obj in graph:
            if not isinstance(obj, dict):
                continue
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if "Event" not in {str(t) for t in types}:
                continue
            start = obj.get("startDate")
            if not start:
                continue
            venue, city = _jsonld_venue_city(obj.get("location"))
            image = obj.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            return {
                "start": str(start),
                "end": str(obj.get("endDate") or "") or None,
                "venue": venue or None,
                "city": city or None,
                "url": (str(obj.get("url") or "").strip() or None),
                "imageUrl": (str(image).strip() if image else None),
            }
    return {}


# --------------------------------------------------------------------------- #
# Per-source best-strategy cache
# --------------------------------------------------------------------------- #

#: Learned ``{slug: {"strategy": ..., "learned": ...}}``. The first time a
#: source's events enrich successfully, the step that filled the fields is
#: recorded and tried first from then on (the "fresh explore", learned from
#: real outcomes rather than a dedicated probe). Manual per-source hints in
#: ``source.json`` (``enrich_strategy``) always win.
_STRATEGIES_PATH = Path(__file__).resolve().parent.parent / "processed" / "enrich_strategies.json"
_STRATEGIES_LOCK = threading.Lock()


def learned_strategies() -> dict[str, str]:
    try:
        data = json.loads(_STRATEGIES_PATH.read_text(encoding="utf-8"))
        return {slug: entry.get("strategy") for slug, entry in data.items()
                if isinstance(entry, dict)}
    except (OSError, json.JSONDecodeError):
        return {}


def _learn_strategy(slug: str, strategy: str) -> None:
    if not slug:
        return
    try:
        with _STRATEGIES_LOCK:
            try:
                data = json.loads(_STRATEGIES_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            data[slug] = {"strategy": strategy,
                          "learned": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            _STRATEGIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STRATEGIES_PATH.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def _step_order(strategy: str) -> list[str]:
    """Enrichment steps to try, preferred strategy first, rest as fallback.

    The auto order puts the rich deterministic parses (ical feeds, JSON-LD
    Event markup) before the URL-date guess — a date-only guess preempts a
    fetch that could also fill venue/city/end."""
    auto = ["links_ical", "jsonld", "page_ical", "url_date", "llm"]
    preferred = {
        "ical": ["links_ical", "page_ical"],
        "jsonld": ["jsonld"],
        "llm": ["llm"],
        "url_date": ["url_date"],
    }.get(strategy)
    if not preferred:
        return auto
    return preferred + [step for step in auto if step not in preferred]


def enrich_event(record: dict[str, Any], chat: llm.ChatClient,
                  detail_links: Optional[list[str]] = None,
                  strategy: str = "auto",
                  source_slug: Optional[str] = None) -> dict[str, Any]:
    """Fill missing event fields, trying the source's best strategy first.

    Steps, in fallback order: an ical feed among ``detail_links`` (no LLM), a
    date embedded in the URL, then the richest fetchable page (ticketing /
    FB-event / event site, else the record's own page) parsed as — JSON-LD
    ``Event`` markup (no LLM), an ical link inside the page (no LLM), the
    touch-up model.

    ``strategy`` names the step to try first (learned per source, or the
    manual ``enrich_strategy`` hint); everything else remains a fallback, so a
    page-shape change self-heals. ``"none"`` skips enrichment entirely.
    """
    if strategy == "none":
        return record
    steps = _step_order(strategy)
    links = [u for u in (detail_links or []) if isinstance(u, str) and u.startswith("http")]

    def missing() -> list[str]:
        return [f for f in ("start", "venue", "city", "category") if not record.get(f)]

    if not missing():
        return record
    filled_by: Optional[str] = None
    fetched = False

    def page_steps(raw: str) -> bool:
        """Run the fetch-page steps on one page; True when the record is complete."""
        nonlocal filled_by
        if "jsonld" in steps:
            fields = _jsonld_event_fields(raw)
            if fields.get("start"):
                for key in ("start", "end", "venue", "city", "url", "imageUrl"):
                    if fields.get(key) and not record.get(key):
                        record[key] = fields[key]
                filled_by = filled_by or "jsonld"
                if not missing():
                    return True
        if "page_ical" in steps and "start" in missing():
            for ical_url in _ICAL_URL_RE.findall(raw)[:2]:
                start, end = _ical_times(ical_url)
                if start:
                    record["start"] = start
                    if end and not record.get("end"):
                        record["end"] = end
                    filled_by = filled_by or "ical"
                    break
        if "llm" in steps and chat is not None and missing():
            text = _html_to_text(raw)
            if not text:
                return False
            user = (
                f"EVENT: {record.get('title')}\n"
                f"Missing fields: {', '.join(missing())}\n\n"
                f"PAGE TEXT:\n{text}"
            )
            try:
                data = chat.complete_json(
                    [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                    TOUCHUP_SCHEMA, schema_name="event_touchup",
                )
            except llm.LLMError:
                return False
            before = {f: bool(record.get(f)) for f in _FIELDS}
            for field in _FIELDS:
                value = data.get(field)
                if isinstance(value, str) and value.strip().lower() not in ("", "null", "none", "n/a"):
                    record.setdefault(field, value.strip())
            if any(record.get(f) and not before[f] for f in _FIELDS):
                filled_by = filled_by or "llm"
            if not missing():
                return True
        return False

    # ical feeds among the candidate's own links (deterministic, no fetch).
    if "links_ical" in steps and "start" in missing():
        for url in [u for u in links if _is_ical_url(u)]:
            start, end = _ical_times(url)
            if start:
                record["start"] = start
                if end and not record.get("end"):
                    record["end"] = end
                filled_by = "ical"
                break

    # fetch the richest page available and parse it (rich deterministic parses
    # first; the URL-date guess comes after so it cannot preempt them).
    if missing():
        for url in [u for u in links if not _is_ical_url(u)] + [record.get("url") or ""]:
            if not url:
                continue
            raw = fetch_html(url)
            if not raw:
                continue
            fetched = True
            if page_steps(raw):
                break

    # a date embedded in the URL (weakest signal: start only, no time/venue).
    if "url_date" in steps and "start" in missing():
        guess = date_from_url(record.get("url") or "")
        if guess:
            record["start"] = guess
            if not filled_by:
                filled_by = "url_date"

    # Learn what worked (or that nothing does) for this source.
    if source_slug:
        if filled_by:
            _learn_strategy(source_slug, filled_by)
        elif fetched:
            _learn_strategy(source_slug, "none")
    return record