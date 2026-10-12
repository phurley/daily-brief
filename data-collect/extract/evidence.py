"""Event facts backed by an individual organizer listing, never adjacent cards."""
from __future__ import annotations

import html
import math
import re
from zoneinfo import ZoneInfo


EVENT_TYPES = {"Event", "MusicEvent", "TheaterEvent", "ComedyEvent", "DanceEvent",
               "EducationEvent", "ExhibitionEvent", "Festival", "ScreeningEvent",
               "SocialEvent", "BusinessEvent", "LiteraryEvent", "SportsEvent"}
_STATUS = re.compile(r"\b(?:this\s+(?:show|event|performance|concert)\s+(?:has\s+been|is)|"
                     r"(?:show|event|performance|concert)\s+(?:has\s+been|is))\s+"
                     r"(cancelled|canceled|postponed|rescheduled|reinstated)\b[^\n!?]*?(?:\.(?=\s+[A-Z]|$)|[!?]|$)", re.I)


def text(value):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)


def event_objects(value):
    """Flatten graphs and explicit sub-events, inheriting their parent metadata.

    A production date range with sub-events is not a continuous performance.
    """
    if isinstance(value, list):
        for item in value:
            yield from event_objects(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if set(types) & EVENT_TYPES:
            children = value.get("subEvent") or value.get("subEvents")
            if children:
                for child in children if isinstance(children, list) else [children]:
                    if isinstance(child, dict):
                        yield from event_objects({**{k: v for k, v in value.items()
                                                     if k not in {"subEvent", "subEvents", "endDate", "@id"}}, **child})
            else:
                yield value
        for item in value.get("@graph", []):
            yield from event_objects(item)


def offer_fields(offers):
    offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
    amounts, currencies, availability = [], set(), []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        currency = offer.get("priceCurrency") or ""
        currencies.add(currency)
        for key in ("price", "lowPrice", "highPrice"):
            raw = offer.get(key)
            try:
                amount = float(str(raw).replace(",", ""))
                if math.isfinite(amount) and amount >= 0:
                    amounts.append(amount)
            except (TypeError, ValueError):
                pass
        if offer.get("availability"):
            availability.append(str(offer["availability"]).rsplit("/", 1)[-1])
    out = {}
    if availability:
        out["availability"] = ", ".join(sorted(set(availability)))
    if amounts and len(currencies) == 1:
        currency = next(iter(currencies))
        prefix = "$" if currency == "USD" else f"{currency} " if currency else ""
        low, high = min(amounts), max(amounts)
        out["price"] = "Free" if low == high == 0 else prefix + f"{low:g}" + (f"–{prefix}{high:g}" if high != low else "")
        if currency == "USD" or low == high == 0:
            out.update(priceMin=low, priceMax=high, priceCurrency=currency or "USD")
    return out


def price_fields(value):
    """Numeric budget facts only for explicit USD amounts or free admission."""
    value = text(value)
    if not value:
        return {}
    out = {"price": value}
    amounts = [float(v.replace(",", "")) for v in re.findall(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)", value)]
    if re.fullmatch(r"free(?: admission)?", value, re.I):
        amounts = [0.0]
    if amounts:
        out.update(priceMin=min(amounts), priceMax=max(amounts), priceCurrency="USD")
    return out


def apply(record, source_text, *, checked_at=None):
    """Explicit postponement notices beat stale generic EventScheduled markup.

    Keep the old occurrence postponed and attach a verified replacement instant;
    collectors can emit a separate rescheduled occurrence without losing a notice.
    """
    source_text = text(source_text)
    notice = _STATUS.search(source_text)
    if notice:
        record["status"] = notice[1].lower().replace("cancelled", "canceled").replace('reinstated','scheduled')
        record["statusEvidence"] = notice[0].strip()
        record["statusEvidenceKind"] = "announcement"
        replacement = re.search(r"\bto\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?[,]?\s*"
                                r"([A-Z][a-z]+\s+\d{1,2},?\s+20\d{2}\s+at\s+\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)", notice[0], re.I)
        if replacement:
            from dateutil.parser import parse
            try:
                dt = parse(replacement[1]).replace(tzinfo=ZoneInfo("America/Detroit"))
                record["replacementStart"] = dt.isoformat()
            except (ValueError, OverflowError):
                pass
    if checked_at:
        record["lastCheckedAt"] = checked_at
    if record.get("price") and not record.get("priceCurrency"):
        record.update(price_fields(record["price"]))
    return record
