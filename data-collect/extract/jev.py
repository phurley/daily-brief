"""Stage 4: the Jev candidate gate — contract, clients, and routing.

Jev (TypeSafe's System One model) returns typed structured decisions with
calibrated confidence and never generates free text, which makes it a natural
fit for the high-volume "is this chunk worth extracting?" decision between the
mechanical funnel and expensive LLM extraction.

Three question types map onto the gate:

    * Choice -> the content kind (event / news / notice / agenda / ...)
    * Noul   -> booleans (is_content, dated)
    * Score  -> relevance 0..4

This module builds the request, parses answers back into
``{field: {value, confidence}}`` decisions, and routes chunks. Clients:

    * :class:`OpenRouterJevClient` — ``POST /api/alpha/decisions`` (default)
    * :class:`TypeSafeJevClient`   — ``POST /v1/systemone`` (native)
    * :class:`StubJevClient`       — deterministic, offline, for tests

The API key is read from ``OPENROUTER_API_KEY`` (or ``TYPESAFE_API_KEY``) after
loading ``.env``. See :func:`make_client`.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from . import env

# --------------------------------------------------------------------------- #
# Task vocabulary
# --------------------------------------------------------------------------- #

KINDS = ["event", "news", "notice", "agenda", "listing", "nav", "other"]
TIMEFRAMES = ["past", "today", "future", "recurring", "undated"]

_KIND_LABELS = {
    "event": "A specific dated happening (concert, meeting, festival, class, game)",
    "news": "Reported news, article, or announcement",
    "notice": "Official or public notice",
    "agenda": "A meeting agenda or minutes",
    "listing": "An index page listing many items",
    "nav": "Site navigation, menu, header, footer, or other chrome",
    "other": "None of the above",
}
_TIMEFRAME_LABELS = {
    "past": "Occurs before today",
    "today": "Occurs today",
    "future": "Occurs after today",
    "recurring": "Repeating or ongoing",
    "undated": "No date present or inferable",
}
_RELEVANCE_LABELS = [
    "Not relevant to a local daily brief",
    "Slightly relevant",
    "Moderately relevant",
    "Very relevant",
    "Essential to a local daily brief",
]

#: The stage-4 task definition. ``type`` values are the model's output domain.
CANDIDATE_GATE: dict[str, Any] = {
    "name": "candidate_gate",
    "version": "1",
    "description": (
        "Given a chunk of crawled web content, decide whether it is substantive "
        "news/event content worth structured extraction, and what kind it is."
    ),
    "fields": {
        "is_content": {
            "type": "bool",
            "question": (
                "Is this substantive content (news, a dated event, a public "
                "notice) rather than navigation, chrome, a policy page, or boilerplate?"
            ),
            "true": "Substantive, brief-worthy content",
            "false": "Navigation, chrome, boilerplate, or reference material",
        },
        "kind": {
            "type": "enum",
            "values": KINDS,
            "labels": _KIND_LABELS,
            "question": "What kind of content is this chunk?",
        },
        "dated": {
            "type": "bool",
            "question": "Does this content contain an explicit or inferable date or time?",
            "true": "A date or time is present or inferable",
            "false": "No date or time",
        },
        "timeframe": {
            "type": "enum",
            "values": TIMEFRAMES,
            "labels": _TIMEFRAME_LABELS,
            "question": "When does the content's subject occur?",
        },
        "relevance": {
            "type": "int",
            "minimum": 0,
            "maximum": 4,
            "labels": _RELEVANCE_LABELS,
            "question": "How relevant is this to a local daily brief?",
        },
    },
}

#: Production triage task (v7): the gate fields plus cardinality, attendability,
#: and the schema hint. One Jev call yields accept/drop, routing size
#: (cardinality), the event-vs-news signal (attend_on_date), and relevance.
#: Tuned on gold/round2: accept precision 0.92 / recall 0.79; router accuracy
#: 0.68; see gold/round2/CHARACTERIZATION-REPORT.md.
_ITEM_COUNT_FIELD = {
    "type": "enum",
    "values": ["0", "1", "2", "3+"],
    "labels": {
        "0": "No extractable event or news story",
        "1": "Exactly one event or news story",
        "2": "Exactly two separate events or news stories",
        "3+": "Three or more separate events or news stories",
    },
    "question": (
        "Count the separate events or news stories this chunk is about: 0, 1, 2, "
        "or 3 or more. Count ONLY the chunk's main content. Ignore site "
        "navigation, menus, headers, footers, sidebars, cookie/consent banners, "
        "ads, and 'related' / 'recommended' / 'more stories' link widgets. A "
        "single article or event page is 1 even if it mentions other events. "
        "Only count 2 or more when the main content itself is a list, calendar, "
        "index, or digest of that many distinct items."
    ),
}
_ATTEND_FIELD = {
    "type": "bool",
    "question": (
        "Does this chunk describe one or more events that a person could "
        "attend or participate in on a specific date (a concert, meeting, "
        "class, festival, game, tour, or similar)?"
    ),
    "true": "At least one attendable dated event",
    "false": "No attendable dated event (news, policy, reference, or no event)",
}
#: Context booleans: not routed on, but they keep `cardinality` stable. Without
#: them the count question collapses to few/list (see CHARACTERIZATION-REPORT).
_IS_LIST_FIELD = {
    "type": "bool",
    "question": (
        "Is this chunk a list, index, calendar, or search-results page that "
        "contains multiple separate events or news items, rather than a single item?"
    ),
    "true": "A list/index of several separate items",
    "false": "A single item, or not a list of items",
}
_IS_SINGLE_EVENT_FIELD = {
    "type": "bool",
    "question": "Is this chunk a single, specific dated event or happening?",
    "true": "One identifiable event with a date",
    "false": "Not one specific event",
}
_IS_SINGLE_NEWS_FIELD = {
    "type": "bool",
    "question": "Is this chunk a single news story or article?",
    "true": "One news story/article",
    "false": "Not one news story",
}
#: A listicle is an *article written as a list* ("5 Ways to ...", "10 Best ...",
#: "7 Events Celebrating ...") where each item is too brief to carry its own
#: date, venue, or details. This is deliberately distinct from ``is_list``: a
#: calendar, index, or events page with complete per-item details is a list but
#: NOT a listicle — those are high-value and must be kept.
_IS_LISTICLE_FIELD = {
    "type": "bool",
    "question": (
        "Is this chunk a listicle — an article written as a numbered or "
        "curated list (e.g. '5 Ways to...', '10 Best...', '7 Events "
        "Celebrating...') whose items are too brief to have their own date, "
        "venue, or details? Calendar and event-listing pages with complete "
        "per-item details are NOT listicles."
    ),
    "true": "A listicle article (curated list, items too thin to extract)",
    "false": "Not a listicle (single story/event, or a detailed listing)",
}
#: Lottery/lotto entries (draw results, winning numbers, jackpot totals) are
#: dropped at the gate: they name no attendable happening and are filler for a
#: local brief. A raffle or prize giveaway tied to a specific event is NOT a
#: lottery entry.
_IS_LOTTERY_FIELD = {
    "type": "bool",
    "question": (
        "Is this chunk about a lottery or lotto \u2014 draw results, winning "
        "numbers, jackpot amounts, or how/where to play? (A raffle or prize "
        "giveaway tied to a specific event is NOT a lottery entry.)"
    ),
    "true": "A lottery/lotto entry, draw, or result",
    "false": "Not lottery/lotto related",
}
#: Sports coverage (scores, fixtures, standings, team/player news, broadcast
#: schedules) is dropped at the gate. A non-sport community event that merely
#: happens at a sports venue is NOT sports coverage.
_IS_SPORTS_FIELD = {
    "type": "bool",
    "question": (
        "Is this chunk primarily about sports \u2014 a game, match, or "
        "competition, a team or player, scores, standings, fixtures, or sports "
        "broadcast coverage? (A non-sport community event held at a sports "
        "venue, or a listing that merely includes a game, is NOT sports coverage.)"
    ),
    "true": "Sports event, team, or sports coverage",
    "false": "Not about sports",
}
#: Journalism about an event (preview/review/feature) rather than the event
#: listing itself. Paired with ``_DETAILS_MISSING_FIELD`` this diverts
#: articles to news extraction instead of producing invalid event records.
_ARTICLE_ABOUT_EVENT_FIELD = {
    "type": "bool",
    "question": (
        "Is this journalism ABOUT an event — a preview, review, feature, or "
        "announcement article — rather than the event's listing itself? "
        "(The event's date/venue may be mentioned only in passing.)"
    ),
    "true": "An article about an event",
    "false": "The event listing itself, or not about an event",
}
#: The event's attendable details (date + venue) are absent from the text.
#: Gate-lab: best single predictor of failed event extraction (P .49 / R .74
#: on re-work outcomes) — usable as a routing signal, not a drop rule.
_DETAILS_MISSING_FIELD = {
    "type": "bool",
    "question": (
        "Does this chunk mention one or more events but NOT state the date "
        "and venue needed to attend each one?"
    ),
    "true": "Event details (date/venue) are missing",
    "false": "Attendable details are stated",
}
_SCHEMA_FIELD = {
    "type": "enum",
    "values": ["event", "news", "notice", "mixed", "none"],
    "labels": {
        "event": "Dated events/happenings",
        "news": "News stories or articles",
        "notice": "Official/public notices",
        "mixed": "A mix of the above",
        "none": "None of these",
    },
    "question": "If extracted, which output schema fits this chunk?",
}

#: Event-scoring rubric: positive and negative qualities asked as Nouls during
#: triage (composite scoring). Weights live in ``extract/scoring.py``. Positives
#: are asked first, then negatives; the order is the order persisted in
#: ``scoring.signals``.
SCORING_QUESTIONS: dict[str, str] = {
    # positive
    "funny": "Is this event comedic, funny, or light-hearted entertainment?",
    "distinctive": "Is this event distinctive, unusual, or a one-of-a-kind experience rather than generic?",
    "live": "Is this a live performance or in-person participation, rather than a screening, recording, or static display?",
    "eclectic": "Is this event eclectic, genre-blending, or hard to put in a single category?",
    "technical_science": "Is this event related to science, technology, engineering, or technical craft?",
    "artsy": "Is this event artistic, creative, or aesthetically oriented?",
    "progressive": "Does this event have a progressive, experimental, or socially forward-leaning character?",
    "outdoors": "Is this event primarily held outdoors or focused on nature \u2014 a hike, trail/nature walk, park, farm, or garden visit?",
    "theatre": "Is this primarily a theatre or stage production \u2014 a play, musical, opera, or dramatic performance?",
    # negative
    "sporting": (
        "Is this primarily a sports event or competition \u2014 a game, match, race, "
        "tournament, or athletic contest? Include team sports, individual "
        "athletics, equestrian/horse shows and racing, motorsports, and similar "
        "competitive events."
    ),
    "recurring": "Is this a recurring, routine event (a weekly/monthly/regular series) rather than a special one-off?",
    "craft_fair_shopping": "Is this primarily a craft fair, market, or shopping/retail event?",
    "religious": "Is this primarily a religious service or faith-based gathering?",
    "large_venue": "Is this event held at a large venue \u2014 an arena, stadium, big amphitheater, convention center, or major festival ground?",
    "substance_recovery": "Is this primarily a substance-use recovery meeting or support group (AA, NA, SMART Recovery, or similar)?",
    "popular_music_cover_band": "Is this primarily live music by a cover or tribute band playing well-known songs?",
    "market_or_shop": "Is this primarily a market or shopping event \u2014 a farmers market, flea market, vendor market, or retail sale?",
    "punk_metal_or_rock": "Is this primarily punk, metal, hard rock, or similar loud/heavy live music?",
    "dance": "Is this primarily a dance event \u2014 a dance party, social dance, or dance class/performance (not a theatre production)?",
    "sales_related": "Is this primarily a sales pitch, promotion, or commercial sales event rather than a community happening?",
    "children_activity": "Is this primarily an activity for young children \u2014 story time, play group, kids' craft, or family/kid-focused program?",
    "running": "Is this primarily a running event \u2014 a race, fun run, 5K/marathon, or training run?",
    "exercise": "Is this primarily an exercise or fitness class/workout \u2014 yoga, aerobics, strength training, or similar?",
    "employment_related": "Is this primarily a job fair, hiring event, career workshop, or employment-related program?",
}

SCORING_TASK: dict[str, Any] = {
    "name": "event_scoring",
    "version": "1",
    "description": "Score an event's fit for a local daily brief (0-100).",
    "fields": {
        name: {
            "type": "bool",
            "question": question,
            "true": "Yes, this quality describes the event",
            "false": "No, this quality does not describe the event",
        }
        for name, question in SCORING_QUESTIONS.items()
    },
}

TRIAGE: dict[str, Any] = {
    "name": "triage",
    "version": "1",
    "description": CANDIDATE_GATE["description"],
    "fields": {
        "is_content": CANDIDATE_GATE["fields"]["is_content"],
        "kind": CANDIDATE_GATE["fields"]["kind"],
        "is_list": _IS_LIST_FIELD,
        "is_listicle": _IS_LISTICLE_FIELD,
        "is_lottery": _IS_LOTTERY_FIELD,
        "is_sports": _IS_SPORTS_FIELD,
        "is_article_about_event": _ARTICLE_ABOUT_EVENT_FIELD,
        "details_missing": _DETAILS_MISSING_FIELD,
        "is_single_event": _IS_SINGLE_EVENT_FIELD,
        "is_single_news": _IS_SINGLE_NEWS_FIELD,
        "item_count": _ITEM_COUNT_FIELD,
        "schema": _SCHEMA_FIELD,
        "attend_on_date": _ATTEND_FIELD,
        "relevance": CANDIDATE_GATE["fields"]["relevance"],
        # Scoring rides along in the same call (composite scoring).
        **SCORING_TASK["fields"],
    },
}

#: Route thresholds. Calibrated confidence is what makes these meaningful.
#: Tuned on gold/round2 (100 labeled chunks): accept>=0.5 gives 0.96 precision /
#: 0.74 recall with only 14/100 escalated; 0.6 gives 1.00 precision / 0.64 recall.
#: 0.5 follows TypeSafe's "genuinely uncertain" floor and favours recall.
ACCEPT_CONFIDENCE = 0.50
DROP_CONFIDENCE = 0.50
RELEVANCE_FLOOR = 2          # below this -> not brief-worthy

#: Fields whose confidence drives routing. Relevance is intentionally excluded:
#: its Score distribution is spread across five levels, so its level confidence
#: is low even when the value is clear. Relevance gates on its value instead.
ROUTING_FIELDS = ("is_content", "kind")

SKIP_KINDS = {"nav", "other"}

#: A confident ``is_listicle`` drops the candidate at the gate: listicle items
#: are too thin to extract (no per-item date/venue) and the articles are
#: low-value filler for a daily brief. Gate-lab on 179 labeled candidates:
#: precision 1.00 at conf >= 0.60 with zero good candidates dropped.
LISTICLE_DROP_CONFIDENCE = 0.60

#: Lottery/lotto and sports coverage are dropped at the gate too. Both are
#: confident-only drops (an uncertain call keeps the candidate) and both need
#: a labeled round before their thresholds can be tuned; 0.60 matches the
#: listicle rule until then.
LOTTERY_DROP_CONFIDENCE = 0.60
SPORTS_DROP_CONFIDENCE = 0.60

#: Route threshold for the article-about-event divert (see ``extract/router``).
#: Gate-lab: diverts 11% of re-work to news with 0/99 clean events lost.
ARTICLE_ROUTE_CONFIDENCE = 0.60

#: Mechanical listicle guess used by the offline stub and as an evaluation
#: baseline: a numbered/curated headline ("5 Ways to ...", "Top 10 ...",
#: "7 Fall Festivals ..."). The Jev question is the real detector; this is
#: only cheap evidence.
_LISTICLE_HEAD_RE = re.compile(
    r"^\W*(?:top\s+|best\s+|our\s+|the\s+)?\d{1,2}\s+(?:[a-z]+\s+){0,2}?"
    r"(?:ways|things|events|reasons|spots|places|restaurants|bars|signs|facts|"
    r"ideas|tips|questions|gifts|books|songs|movies|photos|stories|deals|"
    r"festivals|activities|attractions|destinations|experiences|hidden|"
    r"must|essential)(?!,)",
    re.IGNORECASE,
)


def _chunk_head(state: str, limit: int = 600) -> str:
    """Return the candidate's own text from a chunk state, for stub heuristics."""
    try:
        text = state.split("CHUNK:", 1)[1]
    except IndexError:
        text = state
    return text[:limit]


def _looks_like_listicle(state: str) -> bool:
    """Cheap listicle guess from the head of a chunk state (stub only)."""
    return bool(_LISTICLE_HEAD_RE.match(_chunk_head(state, 120).strip()))


#: Mechanical lottery/sports guesses used by the offline stub only; the Jev
#: questions are the real detectors.
_LOTTERY_RE = re.compile(
    r"\b(?:lottery|lotto|powerball|mega\s?millions|jackpot|"
    r"winning\s+numbers?|draw\s+results?)\b",
    re.IGNORECASE,
)
_SPORTS_RE = re.compile(
    r"\b(?:sports?|football|basketball|baseball|softball|soccer|hockey|"
    r"volleyball|wrestling|lacrosse|golf|tennis|track\s+and\s+field|"
    r"cross[- ]country|varsity|playoffs?|standings|box\s+score|"
    r"vs\.?|athletics?|touchdowns?|home\s+run)\b",
    re.IGNORECASE,
)


def _looks_like_lottery(state: str) -> bool:
    return bool(_LOTTERY_RE.search(_chunk_head(state)))


def _looks_like_sports(state: str) -> bool:
    return bool(_SPORTS_RE.search(_chunk_head(state)))


# --------------------------------------------------------------------------- #
# Question / answer translation
# --------------------------------------------------------------------------- #

def build_questions(task: dict[str, Any] | None = None) -> dict[str, Any]:
    """Translate the task field spec into a Jev ``questions`` payload."""
    spec = (task or CANDIDATE_GATE).get("fields", {})
    questions: dict[str, Any] = {}
    for name, f in spec.items():
        ftype = f.get("type")
        if ftype == "bool":
            questions[name] = {
                "type": "noul",
                "instructions": f["question"],
                "criteria": {"true": f.get("true", "Yes"), "false": f.get("false", "No")},
            }
        elif ftype == "enum":
            labels = f.get("labels", {})
            questions[name] = {
                "type": "choice",
                "instructions": f["question"],
                "criteria": {v: labels.get(v, v) for v in f["values"]},
            }
        elif ftype == "int":
            questions[name] = {
                "type": "score",
                "instructions": f["question"],
                "criteria": list(f["labels"]),
            }
        else:  # pragma: no cover - guarded by the spec
            raise ValueError(f"unsupported field type: {ftype}")
    return questions


def _noul_confidence(value: float) -> float:
    """Noul answers have no confidence; use the probability of the chosen outcome.

    ``max(p, 1-p)`` matches how Choice confidence relates to its top option,
    so the two can be compared in :data:`ROUTING_FIELDS`.
    """
    p = float(value)
    return max(0.0, min(1.0, max(p, 1.0 - p)))


def parse_answers(task: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    """Map raw Jev answers back into ``{field: {value, confidence}}`` decisions."""
    spec = task.get("fields", {})
    decisions: dict[str, Any] = {}
    for name, f in spec.items():
        answer = answers.get(name)
        if not isinstance(answer, dict):
            continue
        ftype = f.get("type")
        if ftype == "bool" and answer.get("type") == "noul":
            raw = answer.get("noul")
            if raw is None:
                continue
            decisions[name] = {
                "value": float(raw) >= 0.5,
                "confidence": _noul_confidence(raw),
                "probability": float(raw),
            }
        elif ftype == "enum" and answer.get("type") == "choice":
            value = answer.get("choice")
            if value is None:
                continue
            decisions[name] = {
                "value": value,
                "confidence": float(answer.get("confidence", 0.0)),
                "probabilities": answer.get("probabilities", {}),
            }
        elif ftype == "int" and answer.get("type") == "score":
            score = answer.get("score")
            if score is None:
                continue
            low, high = f.get("minimum", 0), f.get("maximum", 4)
            decisions[name] = {
                "value": max(low, min(high, round(float(score)))),
                "confidence": float(answer.get("confidence", 0.0)),
                "score": float(score),
                "probabilities": answer.get("probabilities", {}),
            }
    return decisions


# --------------------------------------------------------------------------- #
# Decision records
# --------------------------------------------------------------------------- #

@dataclass
class FieldDecision:
    value: Any
    confidence: float


@dataclass
class ChunkDecision:
    chunk_id: str
    fields: dict[str, FieldDecision] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def min_confidence(self) -> float:
        if not self.fields:
            return 0.0
        return min(f.confidence for f in self.fields.values())

    @property
    def routing_confidence(self) -> float:
        """Confidence over the fields that drive the accept/drop decision."""
        vals = [
            self.fields[name].confidence
            for name in ROUTING_FIELDS
            if name in self.fields
        ]
        return min(vals) if vals else 0.0

    def value(self, name: str, default: Any = None) -> Any:
        fd = self.fields.get(name)
        return fd.value if fd else default


class JevClient(Protocol):
    """Minimal interface the funnel expects from Jev."""

    def decide(self, task: dict[str, Any], state: str) -> dict[str, Any]:
        """Return ``{field: {"value": ..., "confidence": ...}, ...}``."""
        ...


# --------------------------------------------------------------------------- #
# Chunk -> state projection
# --------------------------------------------------------------------------- #

def record_text(record: dict[str, Any]) -> str:
    """Best text for a record: chunk text, or feed fields when there is none."""
    text = record.get("text")
    if text:
        return str(text)
    parts = [record.get("title"), record.get("summary"), record.get("venue")]
    if record.get("author"):
        parts.append(f"author: {record['author']}")
    if record.get("categories"):
        parts.append("categories: " + "; ".join(map(str, record["categories"])))
    return "\n".join(str(p) for p in parts if p)


def chunk_state(record: dict[str, Any]) -> str:
    """Build the model's input state from a stage-3 chunk or feed record.

    Includes cheap mechanical hints so the decision doesn't spend capacity
    re-deriving things the funnel already knows. No raw nav/boilerplate.
    """
    sig = record.get("signals", {})
    loc = sig.get("locality", {})
    header = {
        "source": record.get("source_name"),
        "source_type": record.get("source_type"),
        "url": record.get("url"),
        "url_class": record.get("url_class"),
        "page_title": record.get("page_title"),
        "heading": record.get("heading"),
        "hints": {
            "has_date": sig.get("has_date"),
            "has_time": sig.get("has_time"),
            "dates": sig.get("dates", []),
            "event_terms": sig.get("event_terms", []),
            "news_terms": sig.get("news_terms", []),
            "out_of_area": loc.get("out_of_area"),
        },
    }
    return (
        "CONTEXT:\n"
        + json.dumps(header, ensure_ascii=False)
        + "\n\nCHUNK:\n"
        + record_text(record)
    )


# --------------------------------------------------------------------------- #
# HTTP clients
# --------------------------------------------------------------------------- #

class JevError(RuntimeError):
    """Raised for a non-retryable Jev API failure."""


_RETRYABLE_STATUS = {429, 500, 502, 503, 524, 529}


class HttpJevClient:
    """Base client: JSON POST to a System One endpoint with retry/backoff."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        if not api_key:
            raise JevError("missing API key")
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.extra_headers = extra_headers or {}

    # -- request ----------------------------------------------------------- #
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        last_error = "unknown error"
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(self.endpoint, data=body, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code in _RETRYABLE_STATUS and attempt < self.retries:
                    time.sleep(self._delay(attempt, exc.headers.get("Retry-After")))
                    continue
                raise JevError(last_error) from exc
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(self._delay(attempt, None))
                    continue
                raise JevError(last_error) from exc
        raise JevError(last_error)  # pragma: no cover

    def _delay(self, attempt: int, retry_after: Optional[str]) -> float:
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self.backoff * (2 ** attempt)

    # -- decision ---------------------------------------------------------- #
    def decide(self, task: dict[str, Any], state: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "state": state,
            "questions": build_questions(task),
        }
        data = self._post(payload)
        return parse_answers(task, data.get("answers", {}))

    def decide_raw(self, state: str, questions: dict[str, Any]) -> dict[str, Any]:
        """Low-level call for ad-hoc questions; returns the full response."""
        return self._post({"model": self.model, "state": state, "questions": questions})


class OpenRouterJevClient(HttpJevClient):
    """Jev via OpenRouter's Decisions router."""

    DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
    DEFAULT_MODEL = "typesafe/jev-1.13"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        env.load_dotenv()
        # Accept the misspelling too, in case it is pasted that way.
        key = api_key or env.getenv("OPENROUTER_API_KEY", "OPENROUTE_API_KEY")
        headers = {}
        referer = env.getenv("OPENROUTER_HTTP_REFERER")
        title = env.getenv("OPENROUTER_TITLE")
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
        super().__init__(
            endpoint=endpoint or self.DEFAULT_ENDPOINT,
            model=model or self.DEFAULT_MODEL,
            api_key=key,
            extra_headers=headers,
            **kwargs,
        )


class TypeSafeJevClient(HttpJevClient):
    """Jev via TypeSafe's native endpoint."""

    DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
    DEFAULT_MODEL = "jev-latest"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        env.load_dotenv()
        key = api_key or env.getenv("TYPESAFE_API_KEY")
        super().__init__(
            endpoint=endpoint or self.DEFAULT_ENDPOINT,
            model=model or self.DEFAULT_MODEL,
            api_key=key,
            **kwargs,
        )


def make_client(prefer: Optional[str] = None, **kwargs: Any) -> JevClient:
    """Pick a client: OpenRouter if keyed, else TypeSafe, else the stub."""
    env.load_dotenv()
    if prefer == "openrouter":
        return OpenRouterJevClient(**kwargs)
    if prefer == "typesafe":
        return TypeSafeJevClient(**kwargs)
    if prefer == "stub":
        return StubJevClient()
    if env.getenv("OPENROUTER_API_KEY", "OPENROUTE_API_KEY"):
        return OpenRouterJevClient(**kwargs)
    if env.getenv("TYPESAFE_API_KEY"):
        return TypeSafeJevClient(**kwargs)
    return StubJevClient()


# --------------------------------------------------------------------------- #
# Deterministic stub (no API access)
# --------------------------------------------------------------------------- #

class StubJevClient:
    """Deterministic stand-in built from the funnel's mechanical hints."""

    def decide(self, task: dict[str, Any], state: str) -> dict[str, Any]:
        try:
            ctx_blob = state.split("CHUNK:", 1)[0].split("CONTEXT:", 1)[1].strip()
            ctx = json.loads(ctx_blob)
        except (IndexError, json.JSONDecodeError):
            ctx = {}
        hints = ctx.get("hints", {})
        url_class = ctx.get("url_class", "other")
        has_date = bool(hints.get("has_date"))
        event = len(hints.get("event_terms", []))
        news = len(hints.get("news_terms", []))

        if url_class in ("event",) and (has_date or event):
            kind, conf = "event", 0.9
        elif url_class == "agenda":
            kind, conf = "agenda", 0.85
        elif url_class == "news" and news:
            kind, conf = "news", 0.8
        elif event and has_date:
            kind, conf = "event", 0.75
        elif news >= 2:
            kind, conf = "news", 0.7
        elif url_class in ("utility", "home", "feed", "document") and not (event or news):
            kind, conf = "nav", 0.8
        else:
            kind, conf = "other", 0.4

        is_content = kind not in SKIP_KINDS
        relevance = {"event": 3, "news": 3, "notice": 2, "agenda": 2,
                     "listing": 1, "nav": 0, "other": 0}.get(kind, 0)
        if kind in ("event", "news") and conf < 0.75:
            relevance = min(relevance, 2)
        return {
            "is_content": {"value": is_content, "confidence": conf},
            "kind": {"value": kind, "confidence": conf},
            "is_listicle": {
                "value": _looks_like_listicle(state),
                "confidence": 0.8,
            },
            "is_lottery": {
                "value": _looks_like_lottery(state),
                "confidence": 0.8,
            },
            "is_sports": {
                "value": _looks_like_sports(state),
                "confidence": 0.8,
            },
            "dated": {"value": has_date, "confidence": conf if has_date else 0.6},
            "timeframe": {
                "value": "future" if has_date else "undated",
                "confidence": 0.5 if has_date else 0.4,
            },
            "relevance": {"value": relevance, "confidence": conf},
        }


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #

@dataclass
class GateResult:
    accepted: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    escalated: list[dict[str, Any]] = field(default_factory=list)


def _decision_from_raw(chunk_id: str, raw: dict[str, Any]) -> ChunkDecision:
    fields = {
        name: FieldDecision(value=spec["value"], confidence=float(spec.get("confidence", 0.0)))
        for name, spec in raw.items()
        if isinstance(spec, dict) and "value" in spec
    }
    return ChunkDecision(chunk_id=chunk_id, fields=fields, raw=raw)


@dataclass
class Triage:
    """A production triage decision (task :data:`TRIAGE`)."""
    chunk_id: str
    accepted: bool
    item_count: str           # 0 | 1 | 2 | 3+
    schema_hint: str          # event | news | notice | mixed | none
    attend_on_date: bool
    relevance: int
    kind: str
    confidence: float         # confidence over is_content
    is_listicle: bool = False
    is_lottery: bool = False
    lottery_conf: float = 0.0
    is_sports: bool = False
    sports_conf: float = 0.0
    is_article_about_event: bool = False
    article_conf: float = 0.0
    details_missing: bool = False
    details_conf: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

#: Sentinel for an unanswered optional field (value False, confidence 0).
_NO_FIELD = FieldDecision(value=False, confidence=0.0)


def triage_from_raw(
    chunk_id: str,
    raw: dict[str, Any],
    *,
    accept_confidence: float = ACCEPT_CONFIDENCE,
    relevance_floor: int = RELEVANCE_FLOOR,
) -> Triage:
    """Reduce raw TRIAGE answers to a routable :class:`Triage`."""
    d = _decision_from_raw(chunk_id, raw)
    is_content = bool(d.value("is_content", False))
    kind = d.value("kind", "other")
    relevance = int(d.value("relevance", 0) or 0)
    item_count = d.value("item_count", "0")
    content_conf = d.fields["is_content"].confidence if "is_content" in d.fields else 0.0
    listicle_field = d.fields.get("is_listicle")
    is_listicle = bool(listicle_field.value) if listicle_field else False
    listicle_drop = bool(
        listicle_field
        and listicle_field.value
        and listicle_field.confidence >= LISTICLE_DROP_CONFIDENCE
    )
    lottery_field = d.fields.get("is_lottery")
    lottery_drop = bool(
        lottery_field
        and lottery_field.value
        and lottery_field.confidence >= LOTTERY_DROP_CONFIDENCE
    )
    sports_field = d.fields.get("is_sports")
    sports_drop = bool(
        sports_field
        and sports_field.value
        and sports_field.confidence >= SPORTS_DROP_CONFIDENCE
    )
    accepted = (
        content_conf >= accept_confidence
        and is_content
        and kind not in SKIP_KINDS
        and relevance >= relevance_floor
        and item_count != "0"
        and not listicle_drop
        and not lottery_drop
        and not sports_drop
    )
    return Triage(
        chunk_id=chunk_id,
        accepted=accepted,
        item_count=item_count,
        schema_hint=d.value("schema", "none") or "none",
        attend_on_date=bool(d.value("attend_on_date", False)),
        relevance=relevance,
        kind=kind,
        confidence=content_conf,
        is_listicle=is_listicle,
        is_lottery=bool((d.fields.get("is_lottery") or _NO_FIELD).value),
        lottery_conf=(d.fields.get("is_lottery") or _NO_FIELD).confidence,
        is_sports=bool((d.fields.get("is_sports") or _NO_FIELD).value),
        sports_conf=(d.fields.get("is_sports") or _NO_FIELD).confidence,
        is_article_about_event=bool(
            (d.fields.get("is_article_about_event") or _NO_FIELD).value
        ),
        article_conf=(d.fields.get("is_article_about_event") or _NO_FIELD).confidence,
        details_missing=bool((d.fields.get("details_missing") or _NO_FIELD).value),
        details_conf=(d.fields.get("details_missing") or _NO_FIELD).confidence,
        raw=raw,
    )


def triage_record(
    record: dict[str, Any],
    client: JevClient,
    *,
    accept_confidence: float = ACCEPT_CONFIDENCE,
    relevance_floor: int = RELEVANCE_FLOOR,
) -> Triage:
    """Run the one-call triage task and reduce it to a routable decision."""
    raw = client.decide(TRIAGE, chunk_state(record))
    return triage_from_raw(
        record.get("chunk_id", ""),
        raw,
        accept_confidence=accept_confidence,
        relevance_floor=relevance_floor,
    )


def gate_chunks(
    chunks: Iterable[dict[str, Any]],
    client: JevClient,
    *,
    accept_confidence: float = ACCEPT_CONFIDENCE,
    drop_confidence: float = DROP_CONFIDENCE,
    relevance_floor: int = RELEVANCE_FLOOR,
) -> GateResult:
    """Apply the candidate gate to chunk records and bucket them."""
    result = GateResult()
    for record in chunks:
        decision = _decision_from_raw(
            record.get("chunk_id", ""), client.decide(CANDIDATE_GATE, chunk_state(record))
        )
        kind = decision.value("kind", "other")
        is_content = decision.value("is_content", False)
        relevance = int(decision.value("relevance", 0) or 0)
        conf = decision.routing_confidence

        if conf < accept_confidence:
            result.escalated.append(record)
            continue
        if not is_content or kind in SKIP_KINDS:
            result.dropped.append(record)
            continue
        if relevance < relevance_floor:
            result.dropped.append(record)
            continue
        result.accepted.append(record)
    return result


def load_chunks(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    if path.is_dir():
        path = path / "chunks.jsonl"
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- #
# CLI smoke test
# --------------------------------------------------------------------------- #

_SAMPLES = [
    (
        "event",
        "Kerrytown Concert House",
        "event",
        "Pianist Barron Ryan - Classic Meets Cool. Date: Thursday, September 24, 2026. "
        "Time: 7:30 pm. Tickets $30.",
    ),
    (
        "news",
        "Michigan Daily",
        "news",
        "Regents hear from advocates for gender-affirming care. The University of Michigan "
        "Board of Regents met Thursday, Sept. 18, 2026, to discuss the policy.",
    ),
    (
        "nav",
        "Canton Township",
        "home",
        "I Want To... Apply For Dog License. Contact. Clerk's Office. Finance & Budget. "
        "Parks, Recreation & Community Services. Public Works.",
    ),
    (
        "out-of-area",
        "Canton Focus Patch",
        "event",
        "Bootanical Bash - Bingo & Costume Contest. Sep 26, 2026. Join us in Atlanta, GA "
        "for a fun evening at the botanical garden.",
    ),
]


def _sample_chunk(kind: str, source: str, url_class: str, text: str) -> dict[str, Any]:
    from . import dates, locality  # local import: CLI only

    ref = dates.parse_reference(None)
    signals = {
        "has_date": bool(dates.extract_dates(text, ref=ref)),
        "has_time": any(d.has_time for d in dates.extract_dates(text, ref=ref)),
        "dates": [d.source_text for d in dates.extract_dates(text, ref=ref)],
        "event_terms": [],
        "news_terms": [],
        "locality": locality.detect(text, "https://example.com"),
    }
    return {
        "chunk_id": f"sample-{kind}",
        "source_name": source,
        "source_type": "",
        "url": "https://example.com",
        "url_class": url_class,
        "page_title": source,
        "heading": text[:60],
        "signals": signals,
        "text": text,
    }


def _print_decision(record: dict[str, Any], raw: dict[str, Any]) -> None:
    kind = raw.get("kind", {}).get("value", "?")
    is_content = raw.get("is_content", {}).get("value", "?")
    relevance = raw.get("relevance", {}).get("value", "?")
    conf = min(
        (raw.get(f, {}).get("confidence", 0.0) for f in ROUTING_FIELDS if f in raw),
        default=0.0,
    )
    print(f"\n### {record['chunk_id']} ({record['source_name']})")
    print(f"  is_content={is_content}  kind={kind}  relevance={relevance}  routing_conf={conf:.2f}")
    for name, ans in raw.items():
        detail = ans.get("probabilities") or ans.get("probability") or ans.get("score")
        print(f"    {name}: value={ans['value']!r} conf={ans['confidence']:.2f}  {detail}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m extract.jev", description=__doc__)
    parser.add_argument("--client", choices=["auto", "openrouter", "typesafe", "stub"],
                        default="auto")
    parser.add_argument("--model", default=None, help="override the model id")
    parser.add_argument("--file", type=Path, default=None,
                        help="run against chunks.jsonl (or a processed/<slug> dir)")
    parser.add_argument("--n", type=int, default=3, help="chunks to evaluate from --file")
    opts = parser.parse_args(argv)

    kwargs = {"model": opts.model} if opts.model else {}
    try:
        client = make_client(None if opts.client == "auto" else opts.client, **kwargs)
    except JevError as exc:
        print(f"live Jev unavailable ({exc}); using StubJevClient", file=sys.stderr)
        client = StubJevClient()

    label = type(client).__name__
    print(f"client: {label}  model: {getattr(client, 'model', 'stub')}")

    if isinstance(client, StubJevClient) and opts.client == "auto":
        print("(no OPENROUTER_API_KEY / TYPESAFE_API_KEY found; offline stub)", file=sys.stderr)

    if opts.file:
        chunks = load_chunks(opts.file)[: opts.n]
    else:
        chunks = [_sample_chunk(*s) for s in _SAMPLES]

    for record in chunks:
        raw = client.decide(CANDIDATE_GATE, chunk_state(record))
        _print_decision(record, raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())