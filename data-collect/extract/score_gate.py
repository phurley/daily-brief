#!/usr/bin/env python3
"""Evaluate the Jev candidate gate against a labeled gold set.

Runs the live (or stub) gate over ``candidates.jsonl`` and reports how its
accept / drop / escalate buckets compare to the human ``target`` labels, plus
precision/recall for the accepted set. Decisions can be cached so threshold
sweeps don't re-call the API.

Usage::

    python3 -m extract.score_gate gold/round2 --save
    python3 -m extract.score_gate gold/round2 --decisions gold/round2/gate_decisions.jsonl
    python3 -m extract.score_gate gold/round2 --accept 0.6 --limit 50 --client stub
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any, Optional

from . import jev

SCRIPT_DIR = Path(__file__).resolve().parent.parent


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_gate(
    candidates: list[dict[str, Any]],
    client: jev.JevClient,
    *,
    accept: float,
    relevance_floor: int,
    decisions: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return per-chunk bucket rows and the (possibly cached) decisions."""
    rows: list[dict[str, Any]] = []
    cache = dict(decisions or {})
    for record in candidates:
        cid = record.get("chunk_id", "")
        raw = cache.get(cid)
        if raw is None:
            raw = client.decide(jev.CANDIDATE_GATE, jev.chunk_state(record))
            cache[cid] = raw
        d = jev._decision_from_raw(cid, raw)
        kind = d.value("kind", "other")
        content = bool(d.value("is_content", False))
        relevance = int(d.value("relevance", 0) or 0)
        conf = d.routing_confidence
        if conf < accept:
            bucket = "escalated"
        elif (not content) or kind in jev.SKIP_KINDS or relevance < relevance_floor:
            bucket = "dropped"
        else:
            bucket = "accepted"
        rows.append({
            "chunk_id": cid,
            "bucket": bucket,
            "kind": kind,
            "relevance": relevance,
            "routing_confidence": round(conf, 3),
            "is_content": content,
        })
    return rows, cache


def report(rows: list[dict[str, Any]], labels: list[dict[str, Any]], *, title: str) -> None:
    buckets = collections.Counter(r["bucket"] for r in rows)
    tp = fp = fn = tn = 0
    for row, lab in zip(rows, labels):
        lab = lab.get("label", lab)
        gold = lab.get("target") != "none"
        pred = row["bucket"] == "accepted"
        tp += pred and gold
        fp += pred and not gold
        fn += (not pred) and gold
        tn += (not pred) and not gold
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    print(f"\n=== Jev gate vs {title}: {len(rows)} chunks ===")
    print(f"  buckets: {dict(buckets)}")
    print(f"  accept precision={precision:.2f} recall={recall:.2f} "
          f"(tp={tp} fp={fp} fn={fn} tn={tn})")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("gold_dir", type=Path, help="dir with candidates.jsonl + labels.jsonl")
    p.add_argument("--candidates", type=Path, default=None)
    p.add_argument("--labels", type=Path, default=None)
    p.add_argument("--decisions", type=Path, default=None,
                   help="cache file to read/write decisions")
    p.add_argument("--save", action="store_true", help="write decisions to --decisions")
    p.add_argument("--client", choices=["auto", "openrouter", "typesafe", "stub"], default="auto")
    p.add_argument("--accept", type=float, default=jev.ACCEPT_CONFIDENCE)
    p.add_argument("--relevance-floor", type=int, default=jev.RELEVANCE_FLOOR)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--sweep", action="store_true", help="report several accept thresholds")
    opts = p.parse_args(argv)

    candidates = _load_jsonl(opts.candidates or opts.gold_dir / "candidates.jsonl")
    labels = _load_jsonl(opts.labels or opts.gold_dir / "labels.jsonl")
    if opts.limit:
        candidates, labels = candidates[: opts.limit], labels[: opts.limit]

    decisions = _load_jsonl(opts.decisions) if (opts.decisions and opts.decisions.exists()) else None
    cache = {d["chunk_id"]: d["answers"] for d in decisions} if decisions else None

    client = jev.make_client(None if opts.client == "auto" else opts.client)
    print(f"client: {type(client).__name__}  model: {getattr(client, 'model', 'stub')}")
    rows, cache = run_gate(
        candidates, client,
        accept=opts.accept, relevance_floor=opts.relevance_floor, decisions=cache,
    )

    if opts.sweep:
        for threshold in (0.4, 0.5, 0.6, 0.7, 0.8):
            swept, _ = run_gate(
                candidates, client,
                accept=threshold, relevance_floor=opts.relevance_floor, decisions=cache,
            )
            report(swept, labels, title=f"{opts.gold_dir.name} @ accept>={threshold}")
    else:
        report(rows, labels, title=f"{opts.gold_dir.name} @ accept>={opts.accept}")

    if opts.save and opts.decisions:
        opts.decisions.write_text(
            "\n".join(json.dumps({"chunk_id": c, "answers": a}) for c, a in cache.items()) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {len(cache)} decisions -> {opts.decisions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())