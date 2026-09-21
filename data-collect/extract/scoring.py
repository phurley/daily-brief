#!/usr/bin/env python3
"""Jev event scoring (0-100).

Eleven positive and fifteen negative Noul questions, evaluated in one Jev call
and combined with weights in code (composite scoring). The per-question
probabilities are persisted on every record (and published as a ``scoring``
block) so these weights can be re-tuned against real answers. Locality is deliberately
light; there is no timeliness or family-friendly term (events are shown sorted by
date anyway). Tune the weights below.

    score = clamp(0.5 + Σ pos_weight·p(pos) − Σ neg_weight·p(neg), 0, 1) · 100

A neutral event with no signals scores 50. Nouls return the probability of "yes",
so each term is naturally probabilistic.
"""

from __future__ import annotations

from typing import Any, Optional

from . import jev

#: positive quality -> weight. Boosts the rare, discriminating qualities.
#: Initial values; re-tune against the ``scoring.signals`` published in the JSON.
POS_WEIGHTS: dict[str, float] = {
    "distinctive": 0.24,        # boosted
    "eclectic": 0.16,           # boosted
    "technical_science": 0.14,  # boosted
    "funny": 0.12,
    "artsy": 0.10,
    "progressive": 0.08,
    "theatre": 0.12,
    "outdoors": 0.10,
    "live_music": 0.12,
    "live_comedy": 0.12,
    # Slightly above the market_or_shop penalty below, so a farmers market nets
    # positive while a plain market/shop still loses.
    "farmer_market": 0.26,
}
#: negative quality -> penalty weight. Doubled so the negative categories bite;
#: they may legitimately clamp a score to 0, which is intended.
NEG_WEIGHTS: dict[str, float] = {
    "recurring": 0.30,
    "sporting": 0.30,
    "large_venue": 0.24,
    "craft_fair_shopping": 0.20,
    "religious": 0.16,
    "substance_recovery": 0.24,
    "popular_music_cover_band": 0.20,
    "market_or_shop": 0.24,
    "punk_metal_or_rock": 0.20,
    "dance": 0.16,
    "sales_related": 0.24,
    "children_activity": 0.40,
    "running": 0.20,
    "exercise": 0.20,
    "employment_related": 0.24,
}

#: The questions live in jev so triage can ask them in the same call.
SCORING_TASK: dict[str, Any] = jev.SCORING_TASK

#: The tunable signal set, in ask order. Persisted alongside every record so the
#: weights above can be re-tuned against real answers later.
SIGNAL_NAMES: tuple[str, ...] = tuple(jev.SCORING_QUESTIONS.keys())

#: Neutral starting point and output range for the composite score. Exported for
#: the browser debug page via ``scripts/export_scoring_weights.py``.
BASE = 0.5
SCALE = 100


def build_questions(task: dict[str, Any] | None = None) -> dict[str, Any]:
    return jev.build_questions(task or SCORING_TASK)


def signal_probabilities(raw: dict[str, Any]) -> dict[str, float]:
    """Per-question P(yes) behind the score, compacted for JSON storage."""
    out: dict[str, float] = {}
    for name in SIGNAL_NAMES:
        spec = raw.get(name)
        if isinstance(spec, dict) and spec.get("probability") is not None:
            out[name] = round(float(spec["probability"]), 4)
    return out


def score_probabilities(probabilities: dict[str, float]) -> int:
    """Combine rounded Noul probabilities into a 0-100 score.

    Takes the *stored* (already rounded) signals so the published ``score`` is
    exactly reproducible from ``scoring.signals`` (the JS scoring page and its
    test re-derive it). Missing signals count as 0.
    """
    # Positives then negatives, as declared in POS_WEIGHTS/NEG_WEIGHTS. This is
    # also the order ``scripts/export_scoring_weights.py`` writes, so the JS
    # tuner reproduces scores bit-for-bit (the test checks every event).
    raw = BASE
    for name, weight in POS_WEIGHTS.items():
        raw += weight * float(probabilities.get(name, 0.0) or 0.0)
    for name, weight in NEG_WEIGHTS.items():
        raw -= weight * float(probabilities.get(name, 0.0) or 0.0)
    return max(0, min(SCALE, round(SCALE * min(1.0, max(0.0, raw)))))


def detail(raw: dict[str, Any], *, with_score: bool = False) -> dict[str, Any]:
    """The persisted breakdown: ``{score?, signals}``.

    ``score`` is the event-fit composite, computed from the rounded ``signals``
    so it is reproducible, and is omitted for non-events (the questions are
    event-oriented). ``signals`` is kept for every record so the weights can be
    re-tuned from stored answers.
    """
    signals = signal_probabilities(raw)
    breakdown: dict[str, Any] = {"signals": signals}
    if with_score:
        breakdown["score"] = score_probabilities(signals)
    return breakdown


def score_answers(answers: dict[str, Any]) -> int:
    """Score raw Jev answers; identical to scoring the rounded signals."""
    return score_probabilities(signal_probabilities(answers))


def event_state(record: dict[str, Any]) -> str:
    """Compact state for scoring: the extracted event fields, not the whole chunk."""
    import json

    fields = {k: record.get(k) for k in
              ("title", "summary", "venue", "city", "category", "price", "registration")}
    return "EVENT:\n" + json.dumps(fields, ensure_ascii=False)


def news_state(record: dict[str, Any]) -> str:
    """Compact state for scoring a story: its own fields, not the whole chunk."""
    import json

    fields = {k: record.get(k) for k in ("title", "summary", "topics", "locations")}
    return "STORY:\n" + json.dumps(fields, ensure_ascii=False)


def score_record(record: dict[str, Any], client: jev.JevClient,
                 *, kind: Optional[str] = None) -> tuple[int, dict[str, Any]]:
    """Score one record (event or story) from its own fields.

    Scoring the record rather than the source chunk is what lets a single
    sporting event in a mixed listing register its own ``sporting`` signal.
    """
    kind = kind or record.get("kind") or "event"
    state = event_state(record) if kind == "event" else news_state(record)
    raw = client.decide(SCORING_TASK, state)
    answers = {name: spec for name, spec in raw.items()}
    return score_probabilities(signal_probabilities(answers)), answers


def score_event(record: dict[str, Any], client: jev.JevClient) -> tuple[int, dict[str, Any]]:
    """Back-compat alias for scoring a single event."""
    return score_record(record, client, kind="event")


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    from pathlib import Path

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--records", type=Path,
                   default=Path(__file__).resolve().parent.parent / "processed" / "extracted_records.jsonl")
    p.add_argument("--n", type=int, default=8)
    p.add_argument("--client", choices=["auto", "openrouter", "stub"], default="auto")
    opts = p.parse_args(argv)

    rows = [json.loads(l) for l in opts.records.read_text(encoding="utf-8").splitlines() if l.strip()]
    events = [r for r in rows if r.get("kind") == "event"][: opts.n]
    client = jev.make_client(None if opts.client == "auto" else opts.client)
    print(f"client: {type(client).__name__}  scoring {len(events)} events")
    for r in events:
        score, answers = score_event(r, client)
        top = sorted(answers.items(), key=lambda kv: -kv[1].get("probability", 0))[:3]
        drivers = ", ".join(f"{k}={v['probability']:.2f}" for k, v in top)
        print(f"  {score:3d}  {r.get('title','')[:60]}  [{drivers}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())