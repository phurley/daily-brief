#!/usr/bin/env python3
"""Extraction prompts and JSON schemas for the light extraction model.

Two schemas (event, news) × two shapes (single, array). The schemas ask only
for what the model can know from the text; `id`, `addedAt`, and `localityIndex`
are assigned mechanically by the funnel, not invented here.

They are written to be a subset of the Daily Brief schemas at the repo root
(`schemas/events.schema.json`, `schemas/news.schema.json`) so validation and
merge are mechanical.
"""

from __future__ import annotations

import json
from typing import Any

from . import jev

_SYSTEM = (
    "You extract structured records from crawled local content for a daily brief. "
    "Use only information present in the text. Never invent dates, venues, or "
    "sources. Prefer the most specific value available. If a field is unknown, "
    "omit it or use null. Respond with JSON only."
)

_EVENT_FIELDS = {
    "title": {"type": "string", "description": "The event's name/title."},
    "summary": {"type": "string", "description": "One or two sentences describing the event."},
    "start": {
        "type": ["string", "null"],
        "description": "Absolute start datetime, ISO-8601 with a timezone offset, or null if unknown.",
    },
    "end": {
        "type": ["string", "null"],
        "description": "Absolute end datetime ISO-8601 with offset, or null. Never invent one.",
    },
    "venue": {"type": ["string", "null"], "description": "Venue or place name."},
    "city": {"type": ["string", "null"], "description": "City name."},
    "category": {"type": ["string", "null"], "description": "Short category, e.g. Music, Nature, Government."},
    "price": {"type": ["string", "null"], "description": "Price text, e.g. 'Free' or '$25'."},
    "registration": {"type": ["string", "null"], "description": "Registration or ticket note."},
    "url": {"type": ["string", "null"], "description": "URL for the event."},
    "imageUrl": {"type": ["string", "null"], "description": "Image URL: prefer an image from the text, else CONTEXT.image."},
    "imageAlt": {"type": ["string", "null"], "description": "Short alt text for the image."},
}
_NEWS_FIELDS = {
    "title": {"type": "string", "description": "The story's headline."},
    "summary": {"type": "string", "description": "Two or three sentences summarising the story."},
    "url": {"type": ["string", "null"], "description": "URL for the story."},
    "author": {"type": ["string", "null"], "description": "Author/byline if present."},
    "publication": {"type": ["string", "null"], "description": "Publication name."},
    "publishedAt": {
        "type": ["string", "null"],
        "description": "Absolute publication datetime ISO-8601 with offset, or null.",
    },
    "locations": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Places the story is about.",
    },
    "topics": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Three to six short lowercase topic tags.",
    },
    "imageUrl": {"type": ["string", "null"], "description": "Image URL: prefer an image from the text, else CONTEXT.image."},
    "imageAlt": {"type": ["string", "null"], "description": "Short alt text for the image."},
}

_SCHEMAS = {
    "event_single": {
        "type": "object",
        "additionalProperties": False,
        "properties": _EVENT_FIELDS,
        "required": list(_EVENT_FIELDS),
    },
    "event_array": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "events": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": _EVENT_FIELDS, "required": list(_EVENT_FIELDS),
            }},
        },
        "required": ["events"],
    },
    "news_single": {
        "type": "object",
        "additionalProperties": False,
        "properties": _NEWS_FIELDS,
        "required": list(_NEWS_FIELDS),
    },
    "news_array": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "stories": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": _NEWS_FIELDS, "required": list(_NEWS_FIELDS),
            }},
        },
        "required": ["stories"],
    },
}

_INSTRUCTIONS = {
    "event_single": "Extract the single event described in the text.",
    "event_array": "Extract every distinct event described in the text, up to {cap}.",
    "news_single": "Extract the single news story described in the text.",
    "news_array": "Extract every distinct news story described in the text, up to {cap}.",
}


def schema_for(mode: str) -> dict[str, Any]:
    return _SCHEMAS[mode]


def schema_name(mode: str) -> str:
    return f"daily_brief_{mode}"


def build_messages(mode: str, chunk: dict[str, Any], cap: int = 10) -> list[dict[str, str]]:
    """Return chat messages for extracting one chunk under ``mode``."""
    context = {
        "source": chunk.get("source_name"),
        "source_type": chunk.get("source_type"),
        "page_title": chunk.get("page_title"),
        "url": chunk.get("url"),
        "location": chunk.get("location"),
        # Mechanically normalized dates from the funnel; use these when the text
        # is ambiguous rather than guessing (the biggest benchmark failure mode).
        "known_dates": (chunk.get("signals", {}) or {}).get("normalized_dates", [])[:6],
        # Page/feed image captured by the crawler; prefer an image in the text,
        # else use this.
        "image": chunk.get("page_image") or chunk.get("image") or None,
    }
    instruction = _INSTRUCTIONS[mode].format(cap=cap)
    schema = json.dumps(_SCHEMAS[mode], ensure_ascii=False)
    user = (
        f"{instruction}\n\n"
        f"Return JSON only, matching exactly this schema (use null for unknown, "
        f"do not add other keys):\n{schema}\n\n"
        f"Use CONTEXT.known_dates for a date when the text is ambiguous. "
        f"Use CONTEXT.image for imageUrl when the text has no image of its own.\n\n"
        f"CONTEXT (do not extract records from this):\n{json.dumps(context, ensure_ascii=False)}\n\n"
        f"TEXT:\n{jev.record_text(chunk)}"
    )
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def records_from_response(mode: str, data: Any) -> list[dict[str, Any]]:
    """Pull the record list out of a parsed model response (tolerant of shape)."""
    kind = "event" if mode.startswith("event") else "news"
    key = "events" if mode.startswith("event") else "stories"
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if mode.endswith("_array"):
            items = data.get(key) or data.get("records") or next(
                (v for v in data.values() if isinstance(v, list)), []
            )
        else:
            items = [data]
    else:
        items = []
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("title"):
            out.append({**item, "kind": kind})
    return out