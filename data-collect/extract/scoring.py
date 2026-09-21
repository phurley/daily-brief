#!/usr/bin/env python3
"""Jev event scoring (0-100).

Seven positive and five negative Noul questions, evaluated in one Jev call and
combined with weights in code (composite scoring). The per-question
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
POS_WEIGHTS: dict[str, float] = {
    "distinctive": 0.24,        # boosted
    "eclectic": 0.16,           # boosted
    "technical_science": 0.14,  # boosted
    "live": 0.12,
    "funny": 0.12,
    "artsy": 0.10,
    "progressive": 0.08,
}
#: negative quality -> penalty weight.
NEG_WEIGHTS: dict[str, float] = {
    "recurring": 0.15,
    "sporting": 0.12,
    "large_venue": 0.12,
    "craft_fair_shopping": 0.10,
    "religious": 0.08,
}

#: The questions live in jev so triage can ask them in the same call.
SCORING_TASK: dict[str, Any] = jev.SCORING_TASK

#: The tunable signal set, in ask order. Persisted alongside every record so the
#: weights above can be re-tuned against real answers later.
SIGNAL_NAMES: tuple[str, ...] = tuple(jev.SCORING_QUESTIONS.keys())


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


def detail(raw: dict[str, Any], score: Optional[int] = None) -> dict[str, Any]:
    """The persisted breakdown: ``{score?, signals}``.

    ``score`` is the event-fit composite and is omitted for non-events (the
    questions are event-oriented); ``signals`` is kept for every record so the
    weights can be re-tuned from stored answers.
    """
    breakdown: dict[str, Any] = {"signals": signal_probabilities(raw)}
    if score is not None:
        breakdown["score"] = int(score)
    return breakdown


def score_answers(answers: dict[str, Any]) -> int:
    """Combine Noul probabilities into a 0-100 score (missing answers -> 0)."""
    raw = 0.5
    for name, weight in POS_WEIGHTS.items():
        raw += weight * float(answers.get(name, {}).get("probability", 0.0))
    for name, weight in NEG_WEIGHTS.items():
        raw -= weight * float(answers.get(name, {}).get("probability", 0.0))
    return max(0, min(100, round(100 * min(1.0, max(0.0, raw)))))


def event_state(record: dict[str, Any]) -> str:
    """Compact state for scoring: the extracted event fields, not the whole chunk."""
    import json

    fields = {k: record.get(k) for k in
              ("title", "summary", "venue", "city", "category", "price", "registration")}
    return "EVENT:\n" + json.dumps(fields, ensure_ascii=False)


def score_event(record: dict[str, Any], client: jev.JevClient) -> tuple[int, dict[str, Any]]:
    raw = client.decide(SCORING_TASK, event_state(record))
    answers = {name: spec for name, spec in raw.items()}
    return score_answers(answers), answers


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