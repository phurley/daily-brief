#!/usr/bin/env python3
"""Score a labeled gold set (``processed/gold/labels.jsonl``).

Recomputes the report used to evaluate the mechanical candidate hints against
human labels, so round 1 and round 2 samplings can be compared directly.

Usage::

    python3 -m extract.score_gold
    python3 -m extract.score_gold processed/gold/labels.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LABELS = SCRIPT_DIR / "gold" / "round1" / "labels.jsonl"


def load_labels(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _prf(pred: list[bool], gold: list[bool]) -> tuple[int, int, int, float, float]:
    tp = sum(1 for p, g in zip(pred, gold) if p and g)
    fp = sum(1 for p, g in zip(pred, gold) if p and not g)
    fn = sum(1 for p, g in zip(pred, gold) if not p and g)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return tp, fp, fn, precision, recall


def report(rows: list[dict[str, Any]], *, title: str = "gold set") -> dict[str, Any]:
    n = len(rows)
    labels = [r["label"] for r in rows]
    print(f"\n=== {title}: {n} labeled chunks ===")
    for field in ("target", "kind", "recency", "is_content"):
        dist = collections.Counter(l.get(field) for l in labels)
        pretty = ", ".join(f"{k}={v}" for k, v in dist.most_common())
        print(f"  {field:10s}: {pretty}")

    event_pred = [r.get("candidate_hint") == "event" for r in rows]
    event_gold = [l["target"] == "event" for l in labels]
    news_pred = [r.get("candidate_hint") == "news" for r in rows]
    news_gold = [l["target"] == "news" for l in labels]
    print("\n  mechanical hint vs target:")
    for name, pred, gold in (
        ("hint=event -> target=event", event_pred, event_gold),
        ("hint=news  -> target=news", news_pred, news_gold),
    ):
        tp, fp, fn, p, r = _prf(pred, gold)
        print(f"    {name}: tp={tp} fp={fp} fn={fn} precision={p:.2f} recall={r:.2f}")

    fp_classes = collections.Counter(
        r["url_class"] for r, p, g in zip(rows, event_pred, event_gold) if p and not g
    )
    if fp_classes:
        print(f"    event-hint false positives by url_class: {dict(fp_classes)}")

    actionable = sum(1 for l in labels if l["target"] != "none" and l["recency"] == "current")
    print(
        f"\n  actionable now (target!=none & recency=current): "
        f"{actionable}/{n} = {actionable / n:.0%}"
        if n
        else "  (empty)"
    )
    return {"n": n, "actionable": actionable}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("labels", nargs="?", type=Path, default=DEFAULT_LABELS)
    opts = p.parse_args(argv)
    if not opts.labels.exists():
        print(f"no labels file at {opts.labels}")
        return 1
    report(load_labels(opts.labels), title=opts.labels.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
