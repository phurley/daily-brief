#!/usr/bin/env python3
"""Per-item locality detection.

Round 2's top false-positive source was locality: aggregators like Patch
(serving Illinois/Georgia) and Sun Times (Illinois) return real, current items
that are simply out of area, so ``source.locality_index`` cannot be trusted for
an individual record. This module inspects each item's text and URL and emits a
locality judgement the funnel can gate on.

Output (``detect``)::

    {"places": [...], "tier": 0..4|None, "out_of_area": bool,
     "confidence": 0..1, "reason": "..."}

Tier follows the brief's ``localityIndex``: 0 Canton/Plymouth; 1 immediately
surrounding; 2 Ypsilanti/Ann Arbor; 3 Detroit/metro; 4 statewide. Bias is
*when in doubt, keep more*: an item is only marked out of area on a strong
signal (URL state slug, ``City, ST`` pattern, or a non-Michigan state name) and
only when no in-coverage place is mentioned.
"""

from __future__ import annotations

import re
from typing import Optional

# Coverage gazetteer: place name (lowercase) -> localityIndex tier.
_COVERAGE: dict[str, int] = {}
for _tier, _names in {
    0: ["canton", "canton township", "plymouth", "plymouth township"],
    1: [
        "wayne", "westland", "northville", "belleville", "van buren", "romulus",
        "inkster", "garden city", "livonia", "redford", "plymouth charter township",
    ],
    2: [
        "ypsilanti", "ann arbor", "washtenaw", "saline", "dexter", "chelsea",
        "pittsfield", "superior township", "milan", "dexter township",
    ],
    3: [
        "detroit", "dearborn", "dearborn heights", "warren", "sterling heights",
        "troy", "ferndale", "royal oak", "southfield", "macomb", "oakland",
        "downriver", "grosse pointe", "st. clair shores", "pontiac",
        "auburn hills", "rochester", "novi", "farmington", "madison heights",
        "clawson", "berkley", "hazel park", "hamtramck", "highland park",
    ],
    4: ["michigan"],
}.items():
    for _name in _names:
        _COVERAGE[_name] = _tier

# Full US state names outside Michigan (and DC). Full names are unambiguous.
_OTHER_STATES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
}
# State slugs as they appear in aggregator URL paths (e.g. patch.com/illinois/).
_STATE_SLUGS = {name.replace(" ", "-") for name in _OTHER_STATES}
_STATE_SLUGS |= {"washington-dc", "new-york", "new-jersey", "new-hampshire"}

_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

_CITY_STATE_RE = re.compile(
    r"\b([A-Z][\w'.\-]+(?:\s+[A-Z][\w'.\-]+){0,2}),\s*([A-Z]{2})\b"
)


def _compile(words: set[str]) -> dict[str, re.Pattern[str]]:
    return {
        word: re.compile(rf"(?<![a-z]){re.escape(word)}(?![a-z])", re.IGNORECASE)
        for word in words
    }


_COVERAGE_RE = _compile(set(_COVERAGE))
_OTHER_STATE_RE = _compile(_OTHER_STATES)


def _url_state_slug(url: str) -> Optional[str]:
    for segment in re.split(r"[/?#]", url.lower()):
        if segment in _STATE_SLUGS:
            return segment
    return None


def detect(
    text: str,
    url: str = "",
    *,
    source_tier: Optional[int] = None,
    source_location: str = "",
) -> dict:
    """Infer locality for one item. See module docstring for the output shape."""
    # Source location is a *prior*, not evidence: including it in the haystack
    # would make every item from a Canton-based source look local and mask
    # out-of-area content (the Patch/Sun Times failure mode).
    haystack = f"{text or ''}\n{url or ''}"

    places: list[str] = []
    local_tier: Optional[int] = None
    for name, pattern in _COVERAGE_RE.items():
        if pattern.search(haystack):
            places.append(name)
            tier = _COVERAGE[name]
            if local_tier is None or tier < local_tier:
                local_tier = tier

    # Strong out-of-area signal: an aggregator URL path like patch.com/illinois/.
    # Body state names / "City, ST" are weaker, and a generic "Michigan" mention
    # makes those ambiguous, so we keep ("when in doubt, keep more").
    state_slug = _url_state_slug(url)
    city_state_hits = [
        abbr for _city, abbr in _CITY_STATE_RE.findall(text or "")
        if abbr in _STATE_ABBREVS and abbr != "MI"
    ]
    state_name_hits = [
        name for name, pattern in _OTHER_STATE_RE.items() if pattern.search(text or "")
    ]
    medium_out = bool(city_state_hits or state_name_hits)
    specific_local = local_tier is not None and local_tier <= 3

    if specific_local:
        return {
            "places": sorted(set(places)),
            "tier": local_tier,
            "out_of_area": False,
            "confidence": 0.8,
            "reason": f"local place: {places[0]}",
        }
    if state_slug:
        return {
            "places": sorted(set(places)),
            "tier": None,
            "out_of_area": True,
            "confidence": 0.9,
            "reason": f"url state: {state_slug}",
        }
    if medium_out:
        if local_tier == 4:  # also mentions Michigan -> ambiguous, keep
            return {
                "places": sorted(set(places)),
                "tier": 4,
                "out_of_area": False,
                "confidence": 0.4,
                "reason": "ambiguous state mention",
            }
        reason = (
            f"city,state: {city_state_hits[0]}" if city_state_hits
            else f"state: {state_name_hits[0]}"
        )
        return {
            "places": sorted(set(places)),
            "tier": None,
            "out_of_area": True,
            "confidence": 0.65,
            "reason": reason,
        }
    if local_tier == 4:  # "Michigan" only
        return {
            "places": sorted(set(places)),
            "tier": 4,
            "out_of_area": False,
            "confidence": 0.5,
            "reason": "statewide",
        }
    if source_tier is not None:
        return {
            "places": [],
            "tier": source_tier,
            "out_of_area": False,
            "confidence": 0.35,
            "reason": "source prior",
        }
    return {
        "places": [],
        "tier": None,
        "out_of_area": False,
        "confidence": 0.0,
        "reason": "unknown",
    }