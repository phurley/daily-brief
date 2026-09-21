#!/usr/bin/env python3
"""Export event-fit scoring weights to ``scoring-weights.json``.

``data-collect/extract/scoring.py`` is the single source of truth for the
weights. The browser debug page (``scoring.html``) reads the exported JSON, so
after tuning the weights in Python, refresh the file:

    python3 scripts/export_scoring_weights.py

The page also copes with signals that appear in event data but not in this
export (it adds them with weight 0), so new signals never break the view.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "data-collect"))

from extract import scoring  # noqa: E402


def build_config() -> dict:
    # Preserve Python's summation order (positives then negatives, as declared in
    # scoring.py) so the browser reproduces scores bit-for-bit, not just closely.
    names = [*scoring.POS_WEIGHTS, *scoring.NEG_WEIGHTS]
    for extra in scoring.SIGNAL_NAMES:
        if extra not in names:
            names.append(extra)

    signals: dict[str, dict] = {}
    for name in names:
        if name in scoring.POS_WEIGHTS:
            polarity, weight = "positive", scoring.POS_WEIGHTS[name]
        elif name in scoring.NEG_WEIGHTS:
            polarity, weight = "negative", scoring.NEG_WEIGHTS[name]
        else:
            polarity, weight = "positive", 0.0
        signals[name] = {"weight": weight, "polarity": polarity}
    return {"base": scoring.BASE, "scale": scoring.SCALE, "signals": signals}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "scoring-weights.json")
    opts = parser.parse_args(argv)
    config = build_config()
    opts.out.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Wrote {opts.out} ({len(config['signals'])} signals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())