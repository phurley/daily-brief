#!/usr/bin/env python3
"""Date normalizer for crawled text.

Round-1 labeling showed staleness dominates the corpus (27/80 stale, 36/80
undated of an 80-chunk sample), so the funnel needs to know *when* a chunk's
dates are, in absolute terms, before the Jev gate can filter on recency.

Rules:

* Dates with an explicit offset (``2026-09-14T14:00:00-04:00``, ``...Z``) keep it.
* **Dates without a timezone are assumed to be US Eastern (``America/Detroit``)
  and are DST-aware.**
* Dates without a year use a hint year from the page URL/title if one can be
  found, otherwise the reference date's year, and are flagged ``year_assumed``.
* Recency (``past``/``today``/``future``) is computed against a reference
  instant, defaulting to now.

The module is network-free and dependency-free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/Detroit")

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_RE = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)"
)

# Ordered patterns; each may carry a named `tz` (explicit offset).
_PATTERNS = [
    re.compile(
        r"(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})"
        r"(?:[ T](?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"
        r"\s*(?P<tz>Z|[+-]\d{2}:?\d{2})?)?"
    ),
    re.compile(r"(?P<mo>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{2,4})(?!\d)"),
    re.compile(
        rf"(?P<mon>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?!\d)(?:st|nd|rd|th)?"
        r"(?:\s*,?\s*(?P<y>\d{4}))?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?P<d>\d{{1,2}})(?!\d)(?:st|nd|rd|th)?\s+(?P<mon>{_MONTH_RE})\.?"
        r"(?:\s*,?\s*(?P<y>\d{4}))?",
        re.IGNORECASE,
    ),
]
_TIME_RE = re.compile(
    r"(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)",
    re.IGNORECASE,
)
_HHMM_RE = re.compile(r"\b(?P<h>\d{1,2}):(?P<mi>\d{2})\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass
class NormalizedDate:
    iso: str                 # absolute date-time, with offset
    date: str                # YYYY-MM-DD (Eastern-local)
    dt: datetime             # tz-aware
    has_time: bool
    year_assumed: bool
    tz_assumed: bool
    source_text: str

    @property
    def precision(self) -> str:
        return "minute" if self.has_time else "day"


def _parse_offset(tz: str) -> Optional[timezone]:
    tz = tz.strip()
    if tz in ("Z", "z"):
        return timezone.utc
    m = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", tz)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(sign * delta)


def _resolve_year(raw: Optional[str], ref: datetime, context_year: Optional[int]) -> tuple[int, bool]:
    if raw:
        year = int(raw)
        if year < 100:  # two-digit year: 00-69 -> 2000s, 70-99 -> 1900s
            year += 2000 if year < 70 else 1900
        return year, False
    if context_year:
        return context_year, True
    return ref.astimezone(EASTERN).year, True


def _find_time(window: str) -> tuple[int, int, int] | None:
    m = _TIME_RE.search(window)
    if m:
        hour = int(m.group("h"))
        minute = int(m.group("mi") or 0)
        ampm = m.group("ampm").replace(".", "").lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute, 0
    m = _HHMM_RE.search(window)
    if m:
        hour, minute = int(m.group("h")), int(m.group("mi"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute, 0
    return None


def _build(
    *,
    year: int,
    month: int,
    day: int,
    hour: int | None,
    minute: int | None,
    second: int | None,
    tz: Optional[timezone],
    year_assumed: bool,
    source_text: str,
) -> Optional[NormalizedDate]:
    try:
        zone = tz or EASTERN
        dt = datetime(year, month, day, hour or 0, minute or 0, second or 0, tzinfo=zone)
    except ValueError:
        return None
    return NormalizedDate(
        iso=dt.isoformat(),
        date=dt.astimezone(EASTERN).date().isoformat(),
        dt=dt,
        has_time=hour is not None,
        year_assumed=year_assumed,
        tz_assumed=tz is None,
        source_text=source_text,
    )


def normalize(
    source_text: str,
    *,
    ref: Optional[datetime] = None,
    context_year: Optional[int] = None,
) -> Optional[NormalizedDate]:
    """Parse one date string, assuming Eastern when no offset is present."""
    now = (ref or datetime.now(timezone.utc)).astimezone(EASTERN)
    for pattern in _PATTERNS:
        m = pattern.search(source_text)
        if not m:
            continue
        groups = m.groupdict()
        if groups.get("mon"):
            month = _MONTHS[groups["mon"].lower().rstrip(".")]
        elif groups.get("mo"):
            month = int(groups["mo"])
        else:
            return None
        day = int(groups["d"])
        year, year_assumed = _resolve_year(groups.get("y"), now, context_year)

        tz: Optional[timezone] = None
        if groups.get("tz"):
            tz = _parse_offset(groups["tz"])
        hour = int(groups["h"]) if groups.get("h") else None
        minute = int(groups["mi"]) if groups.get("mi") else None
        second = int(groups["s"]) if groups.get("s") else None

        return _build(
            year=year, month=month, day=day, hour=hour, minute=minute,
            second=second, tz=tz, year_assumed=year_assumed, source_text=source_text,
        )
    return None


def extract_dates(
    text: str,
    *,
    ref: Optional[datetime] = None,
    context_year: Optional[int] = None,
    max_results: int = 12,
) -> list[NormalizedDate]:
    """Find and normalize all absolute dates in ``text`` (deduped, sorted)."""
    now = (ref or datetime.now(timezone.utc)).astimezone(EASTERN)
    found: dict[tuple[str, bool], NormalizedDate] = {}
    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            groups = m.groupdict()
            if groups.get("mon"):
                month = _MONTHS[groups["mon"].lower().rstrip(".")]
            else:
                month = int(groups["mo"])
            day = int(groups["d"])
            year, year_assumed = _resolve_year(groups.get("y"), now, context_year)

            tz: Optional[timezone] = None
            if groups.get("tz"):
                tz = _parse_offset(groups["tz"])
            hour = int(groups["h"]) if groups.get("h") else None
            minute = int(groups["mi"]) if groups.get("mi") else None
            second = int(groups["s"]) if groups.get("s") else None
            if hour is None:
                window = text[m.end(): m.end() + 24]
                parsed = _find_time(window)
                if parsed:
                    hour, minute, second = parsed

            nd = _build(
                year=year, month=month, day=day, hour=hour, minute=minute,
                second=second, tz=tz, year_assumed=year_assumed,
                source_text=m.group(0).strip(),
            )
            if nd is not None:
                found[(nd.iso, nd.has_time)] = nd
    return sorted(found.values(), key=lambda d: d.dt)[:max_results]


def infer_context_year(*texts: str) -> Optional[int]:
    """Best-effort publication/context year from URLs or titles.

    Uses the first 4-digit 19xx/20xx year found in each text (URL first), so
    ``/2026/09/...`` -> 2026 and ``plymouth1890.pdf`` -> 1890. Returns ``None``
    when nothing is found, in which case the reference year is used.
    """
    for text in texts:
        if not text:
            continue
        m = _YEAR_RE.search(text)
        if m:
            return int(m.group(0))
    return None


def _aware(dt: datetime) -> datetime:
    """Attach Eastern to a naive datetime (the default for this corpus)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=EASTERN)


def recency(dt: datetime, ref: Optional[datetime] = None) -> str:
    """Return ``past``, ``today`` or ``future`` for a (possibly naive) datetime."""
    now = ref or datetime.now(timezone.utc)
    ref_local = now.astimezone(EASTERN)
    dt_local = _aware(dt).astimezone(EASTERN)
    if dt_local.date() == ref_local.date():
        return "today"
    return "future" if dt_local > ref_local else "past"


def age_days(dt: datetime, ref: Optional[datetime] = None) -> float:
    """Signed days from ``ref`` to ``dt`` (negative = past)."""
    now = ref or datetime.now(timezone.utc)
    return round((_aware(dt) - now).total_seconds() / 86400, 2)


def summarize(dates: Iterable[NormalizedDate], ref: Optional[datetime] = None) -> dict:
    """Aggregate a set of normalized dates for the funnel's signals."""
    items = list(dates)
    if not items:
        return {
            "date_recency": "none",
            "has_time": False,
            "has_future_date": False,
            "nearest_days_from_ref": None,
            "year_assumed_count": 0,
        }
    counts = {"past": 0, "today": 0, "future": 0}
    for d in items:
        counts[recency(d.dt, ref)] += 1
    if counts["today"]:
        label = "today"
    elif counts["past"] and counts["future"]:
        label = "mixed"
    elif counts["future"]:
        label = "future"
    else:
        label = "past"
    offsets = [age_days(d.dt, ref) for d in items]
    return {
        "date_recency": label,
        "has_time": any(d.has_time for d in items),
        "has_future_date": counts["future"] > 0,
        "nearest_days_from_ref": min(offsets, key=abs),
        "year_assumed_count": sum(1 for d in items if d.year_assumed),
    }


def parse_reference(value: Optional[str]) -> datetime:
    """Parse a ``YYYY-MM-DD`` reference date (Eastern midnight); default now."""
    if not value:
        return datetime.now(timezone.utc)
    d = date.fromisoformat(value)
    return datetime.combine(d, time(0, 0), tzinfo=EASTERN)


def within_window(
    entries: Iterable[tuple[datetime, bool, str]],
    ref: Optional[datetime] = None,
    *,
    known_days: int = 14,
    publish_days: int = 183,
) -> tuple[bool, str]:
    """Two-tier recency filter.

    ``entries`` is an iterable of ``(dt, year_assumed, role)`` where ``role`` is
    ``"event"`` for a **known date** — a date found in the content that probably
    marks a real event, e.g. "Concert on Sept 30" — or ``"publish"`` for a
    **publish date** (RSS ``pubDate`` etc.), which is a weaker signal. Returns
    ``(keep, reason)``.

    * a known event date (``role="event"`` with an explicit year) is allowed up
      to ``known_days`` in the past;
    * a publish date, or any ambiguous date (year had to be assumed), is allowed
      up to ``publish_days`` in the past;
    * **anything in the future is always kept**;
    * undated records are kept (there is nothing to filter on).

    Bias is "when in doubt, keep more": only unambiguous known event dates use
    the strict window; a year-less "Sept 30" is still a content date but falls
    back to the lenient window.
    """
    now = ref or datetime.now(timezone.utc)
    items = [(_aware(dt), year_assumed, role) for dt, year_assumed, role in entries]
    if not items:
        return True, "undated"
    for dt, year_assumed, role in items:
        if dt > now:
            return True, "future"
        strict = role == "event" and not year_assumed
        allowed = known_days if strict else publish_days
        if (now - dt).total_seconds() <= allowed * 86400:
            return True, f"{role}_within_{allowed}d"
    if any(role == "event" and not ya for _, ya, role in items):
        return False, f"event_older_than_{known_days}d"
    return False, f"publish_older_than_{publish_days}d"