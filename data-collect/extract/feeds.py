#!/usr/bin/env python3
"""Mechanical RSS/Atom/ICS feed parser.

The crawler renders fetched feeds to markdown (see ``crawl_sources._parse_feed``
and ``_parse_ics``). This module reverses that rendering back into structured
records, because round-1 labeling showed feeds are the most mechanical,
highest-value source in the corpus: an RSS entry already carries most of the
``news.schema.json`` story fields, and an ICS VEVENT maps to an event.

Rendered formats parsed here::

    # Feed: <url>

    - Mon, 08 Jun 2026 14:34:15 +0000 | Title | https://example.com/post
      Summary text (already truncated to 400 chars by the crawler).
    - <empty date> | Title | https://example.com/post

    # Calendar feed (ICS): <url>

    - 2026-09-14 14:00Z to 2026-09-14 15:30Z: Event title @ Venue -- description
    - 2026-09-14: All-day title @ Venue

The parser is network-free: it reads only the already-crawled markdown.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from . import dates as _dates

RSS_HEADING = "# Feed:"
ICS_HEADING = "# Calendar feed (ICS):"
JSONLD_HEADING = "# Structured data (JSON-LD):"
RSS_EMPTY = "(no feed entries found)"
ICS_EMPTY = "(no VEVENT entries found)"

# Raw XML feeds: some feed URLs are served as text/html (or fetched as pages), so
# the crawler stores the unrendered RSS/Atom instead of ``# Feed:`` markdown.
RAW_FEED_RE = re.compile(r"<(rss|feed|item|entry)[\s>]", re.IGNORECASE)

FEED_ITEM_RE = re.compile(
    r"^-\s+(?P<date>[^|]*)\|\s*(?P<title>.*?)\s*\|\s*(?P<link>https?://\S+)\s*$"
)
#: Enriched continuation line: `  author: …` / `  summary: …`.
FEED_CONT_RE = re.compile(r"^\s{2,}([A-Za-z_]+):\s?(.*)$")
_FEED_KEYS = {"author", "categories", "image", "media", "summary", "content"}
ICS_ITEM_RE = re.compile(r"^-\s+(?P<when>.*?):\s+(?P<rest>.*)$")


def _ical_param(keys_value: tuple[str, str]) -> bool:
    """True for an ical-ish query parameter: Squarespace ``format=ical``,
    The Events Calendar ``ical=1`` / ``?ical``."""
    key, value = keys_value
    key, value = key.lower(), (value or "").lower()
    if key in ("format", "feed", "type"):
        return value == "ical"
    return key.startswith("ical")


def _event_url_from_ical_url(feed_url: str) -> Optional[str]:
    """Per-event ICS feeds point at the event page once the ical flag is
    stripped: Squarespace ``/events/x?format=ical`` -> ``/events/x``, The
    Events Calendar ``/event/x/?ical=1`` -> ``/event/x/``. Returns None for
    calendar URLs without an ical-ish query (a whole ``.ics`` file), where
    the items have no URL of their own.

    The whole query is dropped: per-event ical links sometimes carry extra
    parameters (``?ical=1&post_type=tribe_events``) that are not part of the
    event page URL."""
    if not feed_url:
        return None
    parts = urlsplit(feed_url)
    if parts.scheme == "webcal":
        parts = parts._replace(scheme="https")
    if not any(_ical_param(kv) for kv in parse_qsl(parts.query, keep_blank_values=True)):
        return None
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))


def feed_kind(body: str) -> Optional[str]:
    """Return ``'rss'``, ``'ics'``, ``'jsonld'``, ``'raw'``, or ``None``."""
    stripped = body.lstrip()
    if stripped.startswith(RSS_HEADING) or RSS_EMPTY in body:
        return "rss"
    if stripped.startswith(ICS_HEADING) or ICS_EMPTY in body:
        return "ics"
    if stripped.startswith(JSONLD_HEADING):
        return "jsonld"
    if RAW_FEED_RE.search(body):
        return "raw"
    return None


def normalize_date(raw: str) -> Optional[str]:
    """Parse an RFC-822 or ISO-8601 timestamp to ISO-8601, preserving offset."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dates.EASTERN)  # no offset -> assume Detroit
    return dt.isoformat()


def normalize_ics_datetime(raw: str) -> Optional[str]:
    """Parse the crawler's ICS datetime render (``YYYY-MM-DD HH:MM[Z]``)."""
    raw = (raw or "").strip()
    if not raw or raw == "(no date)":
        return None
    utc = raw.endswith("Z")
    core = raw[:-1] if utc else raw
    if " " in core:
        date_part, time_part = core.split(" ", 1)
        if len(time_part) == 5:  # HH:MM
            time_part += ":00"
        candidate = f"{date_part}T{time_part}"
    elif len(core) == 10:  # YYYY-MM-DD (all-day)
        candidate = f"{core}T00:00:00"
    else:
        return None
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc if utc else _dates.EASTERN)
    return dt.isoformat()


def _item_id(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _feed_url(body: str, fallback: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(RSS_HEADING):
            rest = s[len(RSS_HEADING):].strip()
            # Strip the enriched header suffix: "URL   (rss; N items; latest …)".
            rest = re.split(r"\s{2,}\(", rest)[0].strip()
            return rest or fallback
        if s.startswith(ICS_HEADING):
            return s[len(ICS_HEADING):].strip() or fallback
    return fallback


# ---------------------------------------------------------------------------
# Raw (unrendered) RSS/Atom XML fallback
# ---------------------------------------------------------------------------

_RAW_ITEM_RE = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)


def _xml_text(fragment: str) -> str:
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", fragment, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _raw_tag(fragment: str, *tags: str) -> str:
    for tag in tags:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", fragment, re.IGNORECASE | re.DOTALL)
        if m:
            value = _xml_text(m.group(1))
            if value:
                return value
    return ""


def _raw_link(fragment: str) -> str:
    m = re.search(r"<link\b[^>]*>(.*?)</link>", fragment, re.IGNORECASE | re.DOTALL)
    if m:
        value = _xml_text(m.group(1))
        if value:
            return value
    m = re.search(r"<link\b[^>]*href=[\"']([^\"']+)[\"']", fragment, re.IGNORECASE)
    return m.group(1) if m else ""


def parse_raw_feed_markdown(
    body: str, fallback_url: str = ""
) -> list[dict[str, Any]]:
    """Parse RSS ``<item>`` / Atom ``<entry>`` XML that reached content.md raw."""
    out: list[dict[str, Any]] = []
    for match in _RAW_ITEM_RE.finditer(body):
        fragment = match.group(2)
        title = _raw_tag(fragment, "title")
        link = _raw_link(fragment)
        date_raw = _raw_tag(
            fragment, "pubDate", "published", "updated", "dc:date", "date"
        )
        summary = _raw_tag(
            fragment, "description", "summary", "content:encoded", "content", "encoded"
        )
        if not (title or link):
            continue
        out.append({
            "item_id": _item_id("rss", fallback_url, link, title),
            "feed_kind": "rss",
            "feed_url": fallback_url,
            "title": title,
            "url": link,
            "publishedAt": normalize_date(date_raw),
            "published_raw": date_raw,
            "summary": summary,
            "is_comment": "#comment-" in link,
        })
    return out


def parse_rss_markdown(body: str, fallback_url: str = "") -> list[dict[str, Any]]:
    feed_url = _feed_url(body, fallback_url)
    items: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for line in body.splitlines():
        m = FEED_ITEM_RE.match(line)
        if m:
            if cur:
                items.append(cur)
            cur = {
                "title": m.group("title").strip(),
                "url": m.group("link").strip(),
                "published_raw": m.group("date").strip(),
                "fields": {},
                "free": [],
            }
        elif cur is not None and line[:1].isspace() and line.strip():
            cm = FEED_CONT_RE.match(line)
            key = cm.group(1).lower() if cm else None
            if key in _FEED_KEYS:
                cur["fields"].setdefault(key, []).append(cm.group(2).strip())
            else:  # legacy free-form summary line
                cur["free"].append(line.strip())
    if cur:
        items.append(cur)

    out: list[dict[str, Any]] = []
    for it in items:
        title = it["title"]
        url = it["url"]
        if not (title or url):
            continue
        fields = it["fields"]
        summary = " ".join(fields.get("summary", []) or it["free"]).strip()
        if not summary and fields.get("content"):
            summary = " ".join(fields["content"]).strip()
        categories = [c for part in fields.get("categories", []) for c in part.split(";") if c.strip()]
        out.append({
            "item_id": _item_id("rss", feed_url, url, title),
            "feed_kind": "rss",
            "feed_url": feed_url,
            "title": title,
            "url": url,
            "publishedAt": normalize_date(it["published_raw"]),
            "published_raw": it["published_raw"],
            "summary": summary,
            "author": (fields.get("author") or [""])[0].strip(),
            "categories": categories,
            "image": (fields.get("image") or [""])[0].strip(),
            "media": (fields.get("media") or ["article"])[0].strip(),
            "is_comment": url.startswith("http") and "#comment-" in url,
        })
    return out


def parse_ics_markdown(body: str, fallback_url: str = "") -> list[dict[str, Any]]:
    feed_url = _feed_url(body, fallback_url)
    out: list[dict[str, Any]] = []
    last: Optional[dict[str, Any]] = None
    for line in body.splitlines():
        m = ICS_ITEM_RE.match(line)
        if m:
            when = m.group("when").strip()
            rest = m.group("rest").strip()
            # Format: `{summary} @ {location} -- {description}` (parts optional).
            description = ""
            if " -- " in rest:
                rest, description = rest.split(" -- ", 1)
            location = ""
            if " @ " in rest:
                rest, location = rest.split(" @ ", 1)
            title = rest
            start_raw, end_raw = when, ""
            if " to " in when:
                start_raw, end_raw = when.split(" to ", 1)
            if not title:
                continue
            item = {
                "item_id": _item_id("ics", feed_url, start_raw, title),
                "feed_kind": "ics",
                "feed_url": feed_url,
                "title": title.strip(),
                "url": None,
                "start": normalize_ics_datetime(start_raw),
                "end": normalize_ics_datetime(end_raw),
                "venue": location.strip(),
                "summary": description.strip(),
            }
            out.append(item)
            last = item
            continue
        # Continuation lines (same shape as the RSS render): `  url: …` /
        # `  uid: …` following the item they belong to.
        cont = FEED_CONT_RE.match(line)
        if cont and last is not None:
            key, value = cont.group(1).lower(), cont.group(2).strip()
            if key == "url" and value.startswith("http") and not last.get("url"):
                last["url"] = value
            elif key == "uid" and value and not last.get("_uid"):
                last["_uid"] = value
    for item in out:
        # The Events Calendar UIDs are ``post_id-start-end@host``; WordPress
        # resolves ``/?p=post_id`` to the event's pretty URL, so a site-wide
        # ical without per-VEVENT URLs still yields usable event page links.
        uid = item.pop("_uid", None)
        if not item.get("url") and uid:
            m = re.match(r"^(\d+)-\d+-\d+@([\w.-]+)$", uid)
            if m:
                item["url"] = f"https://{m.group(2)}/?p={m.group(1)}"
    if len(out) == 1:
        # A per-event ICS block (Squarespace ``?format=ical`` etc.) describes
        # one event; its page URL is the feed URL minus the ical flag. This
        # joins the item to the crawled page and satisfies publish's url field.
        if not out[0].get("url"):
            out[0]["url"] = _event_url_from_ical_url(feed_url)
    return out


def _jsonld_heading_url(body: str, fallback: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(JSONLD_HEADING):
            return s[len(JSONLD_HEADING):].strip() or fallback
    return fallback


def _jsonld_venue(location: Any) -> str:
    """schema.org location: a string, a Place, a PostalAddress, or a list."""
    if isinstance(location, str):
        return location.strip()
    if isinstance(location, dict):
        name = location.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return _jsonld_venue(location.get("address"))
    if isinstance(location, list) and location:
        return _jsonld_venue(location[0])
    return ""


def _iso_or_none(value: Any) -> Optional[str]:
    """schema.org startDate/endDate -> ISO-8601 (naive date -> Eastern)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) == 10:  # date-only (all-day event)
        raw += "T00:00:00"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dates.EASTERN)
    return dt.isoformat()


def parse_jsonld_markdown(body: str, fallback_url: str = "") -> list[dict[str, Any]]:
    """Parse the crawler's rendered JSON-LD blocks into event feed items.

    A schema.org ``Event`` object carries exactly the fields whose absence
    causes failed event extraction: ``startDate``/``endDate``, the venue
    (``location.name``) and the event's own URL. Like ICS items, the dates are
    authoritative, so downstream they are treated as structured events.
    """
    feed_url = _jsonld_heading_url(body, fallback_url)
    out: list[dict[str, Any]] = []
    for block in re.findall(r"```json\n(.*?)```", body, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
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
            title = html.unescape(str(obj.get("name") or "")).strip()
            if not title:
                continue
            start = _iso_or_none(obj.get("startDate"))
            summary = html.unescape(str(obj.get("description") or ""))
            summary = re.sub(r"<[^>]+>", " ", summary)
            out.append({
                "item_id": _item_id("jsonld", feed_url, str(obj.get("startDate") or ""), title),
                "feed_kind": "jsonld",
                "feed_url": feed_url,
                "title": title,
                "url": str(obj.get("url") or "").strip() or None,
                "start": start,
                "end": _iso_or_none(obj.get("endDate")),
                "venue": _jsonld_venue(obj.get("location")),
                "summary": re.sub(r"\s+", " ", summary).strip(),
            })
    return out


def parse_feed_markdown(body: str, fallback_url: str = "") -> tuple[str, list[dict[str, Any]]]:
    """Return ``(kind, records)`` for a feed block; kind is '' if none."""
    kind = feed_kind(body)
    if kind == "rss":
        return "rss", parse_rss_markdown(body, fallback_url)
    if kind == "ics":
        return "ics", parse_ics_markdown(body, fallback_url)
    if kind == "jsonld":
        return "jsonld", parse_jsonld_markdown(body, fallback_url)
    if kind == "raw":
        return "rss", parse_raw_feed_markdown(body, fallback_url)
    return "", []


def dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate entries within one source (same url+title, or same id)."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        if it.get("feed_kind") in ("ics", "jsonld"):
            key = it.get("item_id")
        else:
            key = it.get("url") or it.get("item_id")
        norm = re.sub(r"\s+", " ", str(key)).strip().lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(it)
    return out