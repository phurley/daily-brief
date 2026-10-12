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
signal. An explicit out-of-state URL takes precedence over place-name matches;
state-qualified homonyms such as ``Chelsea, NY`` are not Michigan evidence.
Other state mentions can coexist with a separately identified local place.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlsplit

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
        "wayne county",
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
    r"\b([A-Z][\w'.\-]+(?:[ \t]+[A-Z][\w'.\-]+){0,2}),[ \t]*([A-Z]{2})\b"
)
_CITY_STATE_NAME_RE = re.compile(
    r"\b([A-Z][\w'.\-]+(?:[ \t]+[A-Z][\w'.\-]+){0,2}),[ \t]*("
    + "|".join(re.escape(name) for name in sorted(_OTHER_STATES | {"michigan"}, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def _compile(words: set[str]) -> dict[str, re.Pattern[str]]:
    return {
        word: re.compile(rf"(?<![a-z]){re.escape(word)}(?![a-z])", re.IGNORECASE)
        for word in words
    }


_COVERAGE_RE = _compile(set(_COVERAGE))
_COVERAGE_RE["wayne"] = re.compile(_COVERAGE_RE["wayne"].pattern + r"(?![ \t-]+county\b)", re.IGNORECASE)
_OTHER_STATE_RE = {
    name: re.compile(pattern.pattern + r"(?![ \t]+(?:township|university|road|street|avenue)\b)", re.IGNORECASE)
    for name, pattern in _compile(_OTHER_STATES).items()
}


def _url_state_slug(url: str) -> Optional[str]:
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    # Query/fragment links and the hostname are not the article's geography.
    for segment in unquote(path).lower().split("/"):
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
    # A geographic URL scope is stronger than an unqualified Michigan name.
    # Patch's /new-york/chelsea-ny/ must not become local because of "Chelsea".
    state_slug = _url_state_slug(url)
    if state_slug:
        return {
            "places": [],
            "tier": None,
            "out_of_area": True,
            "confidence": 0.9,
            "reason": f"url state: {state_slug}",
        }

    qualified = [
        (city, state) for city, state in _CITY_STATE_RE.findall(text or "")
        if state in _STATE_ABBREVS
    ] + _CITY_STATE_NAME_RE.findall(text or "")
    foreign_places, michigan_places = set(), set()
    qualified_out = []
    qualified_michigan = False
    for city, state in qualified:
        local = state.lower() in {"mi", "michigan"}
        names = {name for name, pattern in _COVERAGE_RE.items()
                 if name != "michigan" and pattern.search(city)}
        (michigan_places if local else foreign_places).update(names)
        qualified_michigan |= local
        # Comma-separated state lists are not City, State pairs. Full names
        # disambiguate covered municipalities; abbreviations identify any city.
        if not local and (state in _STATE_ABBREVS or names):
            qualified_out.append(state)

    state_name_hits = [
        name for name, pattern in _OTHER_STATE_RE.items() if pattern.search(text or "")
    ]
    places: list[str] = []
    local_tier: Optional[int] = None
    try:
        url_path = unquote(urlsplit(url).path)
    except ValueError:
        url_path = ""
    for haystack in (text or "", url_path):
        for name, pattern in _COVERAGE_RE.items():
            # Disambiguation applies to repeated title/summary mentions too.
            if name in foreign_places and name not in michigan_places:
                continue
            if pattern.search(haystack):
                places.append(name)
                tier = _COVERAGE[name]
                if local_tier is None or tier < local_tier:
                    local_tier = tier
        # A local source URL is a fallback; it cannot outrank the item's places.
        if local_tier is not None or qualified_out or state_name_hits or qualified_michigan:
            break
    if local_tier is None and qualified_michigan:
        local_tier = 4  # Explicit Michigan place outside the metro gazetteer.

    # General state mentions may describe a visitor or a comparison. Keep those
    # when there is separate Michigan evidence; qualified locations are stronger.
    medium_out = bool(qualified_out or state_name_hits)
    specific_local = local_tier is not None and local_tier <= 3

    if specific_local:
        return {
            "places": sorted(set(places)),
            "tier": local_tier,
            "out_of_area": False,
            "confidence": 0.8,
            "reason": f"local place: {places[0]}",
        }
    if medium_out:
        if local_tier == 4 and (not qualified_out or qualified_michigan):
            return {
                "places": sorted(set(places)),
                "tier": 4,
                "out_of_area": False,
                "confidence": 0.4,
                "reason": "ambiguous state mention",
            }
        reason = (
            f"city,state: {qualified_out[0]}" if qualified_out
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
