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


def enrich_event(record: dict[str, Any], chat: llm.ChatClient) -> dict[str, Any]:
    missing = [f for f in ("start", "venue", "city", "category") if not record.get(f)]
    if not missing:
        return record
    if "start" in missing:
        guess = date_from_url(record.get("url") or "")
        if guess:
            record["start"] = guess
            missing.remove("start")
    if not missing:
        return record
    text = fetch_text(record.get("url") or "")
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