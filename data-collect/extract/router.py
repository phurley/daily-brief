#!/usr/bin/env python3
"""Stage 6: deterministic router from a triage decision to an extraction task.

The Jev :class:`~extract.jev.Triage` gives `cardinality`, `attend_on_date`, and
`accepted`; this module turns those into an extraction action. It is pure code —
no model call — so it is easy to test and re-tune.

Routing table:

    item_count    attend_on_date   action
    ----------    --------------   ------------------------------------------
    0             *                skip
    1             true             events, one record
    1             false            news, one record
    2             true             events, array (cap 2)
    2             false            news, array (cap 2)
    3+            true             events, array (cap 12)
    3+            false            news, array (cap 12)

`attend_on_date` is the event-vs-news discriminator (P~0.7 / R~0.95 on the gold
set); `cardinality` decides one record vs an array. `schema_hint` is logged but
not authoritative — the extraction model tags each record and validation
enforces the schema.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import jev

RECORD_CAP = {"1": 1, "2": 2, "3+": 12}


@dataclass
class Route:
    action: str          # "extract" | "skip"
    schema: str          # "event" | "news" | "none"
    mode: str            # "event_single" | "event_array" | "news_single" | "news_array" | "skip"
    record_cap: int
    reason: str


def route(triage: jev.Triage, content_mode: str = "auto") -> Route:
    """Map a triage decision to a deterministic extraction route.

    ``content_mode`` is the per-source override from the catalog: ``news``/
    ``event`` force the route (for article-heavy or pure-calendar sources);
    ``auto`` (default) decides per candidate.
    """
    if not triage.accepted or triage.item_count == "0":
        return Route("skip", "none", "skip", 0, f"not accepted (kind={triage.kind})")
    # News digests that merely mention dates fire attend_on_date; divert only the
    # explicit news+news case (sweep showed broader rules cost event recall).
    news_digest = triage.kind == "news" and triage.schema_hint == "news"
    schema = "event" if (triage.attend_on_date and not news_digest) else "news"
    # Gate-lab rule: journalism about an event whose attendable details are
    # missing from the text reliably fails event extraction (no date/venue to
    # extract) — divert to news. On 179 labeled candidates this moves 11% of
    # re-work records to news with 0/99 clean events lost.
    if (
        schema == "event"
        and triage.is_article_about_event
        and triage.article_conf >= jev.ARTICLE_ROUTE_CONFIDENCE
        and triage.details_missing
        and triage.details_conf >= jev.ARTICLE_ROUTE_CONFIDENCE
    ):
        schema = "news"
    if content_mode == "news":
        schema = "news"
    elif content_mode == "event":
        schema = "event"
    array = triage.item_count in ("2", "3+")
    mode = f"{schema}_{'array' if array else 'single'}"
    cap = RECORD_CAP[triage.item_count]
    reason = (
        f"item_count={triage.item_count} attend={triage.attend_on_date} "
        f"kind={triage.kind} schema={triage.schema_hint} relevance={triage.relevance}"
    )
    if schema == "news" and triage.attend_on_date and not news_digest:
        reason += " article_divert"
    if content_mode != "auto":
        reason += f" forced_{content_mode}"
    return Route("extract", schema, mode, cap, reason)