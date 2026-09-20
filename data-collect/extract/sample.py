#!/usr/bin/env python3
"""Stratified chunk sampler for building a labeled gold set.

Reads ``processed/<slug>/chunks.jsonl`` and emits a readable, deterministic
batch of chunks for hand-labeling. Sampling is stratified by ``(url_class,
candidate_hint)`` so every combination gets representation, with a per-source
cap to avoid one prolific source dominating.

Usage::

    python3 -m extract.sample                      # 40 chunks to stdout
    python3 -m extract.sample --n 80 --seed 7
    python3 -m extract.sample --out processed/gold/candidates.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROCESSED = SCRIPT_DIR / "processed"
UTC = timezone.utc


def load_all(processed_dir: Path, *, include_dropped: bool = False) -> list[dict[str, Any]]:
    patterns = ["*/chunks.jsonl"]
    if include_dropped:
        patterns.append("*/chunks_dropped.jsonl")
    chunks: list[dict[str, Any]] = []
    for pattern in patterns:
        for path in sorted(processed_dir.glob(pattern)):
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        chunks.append(json.loads(line))
    return chunks


def corpus_snapshot(processed_dir: Path) -> dict[str, Any]:
    """Identify the corpus a sample was drawn from, so round 1 and round 2
    label sets can be compared apples-to-apples."""
    slugs: list[str] = []
    fingerprints: dict[str, str] = {}
    for path in sorted(processed_dir.glob("*/funnel.json")):
        try:
            stats = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        slug = stats.get("slug", path.parent.name)
        slugs.append(slug)
        fingerprints[slug] = stats.get("fingerprint", "")
    digest = hashlib.sha1(
        "\n".join(f"{s}:{fingerprints[s]}" for s in sorted(fingerprints)).encode()
    ).hexdigest()[:16]
    return {
        "source_count": len(slugs),
        "sources": slugs,
        "fingerprints_sha1": digest,
        "indexed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def stratified_sample(
    chunks: Iterable[dict[str, Any]],
    n: int,
    *,
    seed: int = 0,
    per_source_cap: int = 3,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in chunks:
        buckets[(c.get("url_class", "?"), c.get("candidate_hint", "?"))].append(c)
    for items in buckets.values():
        rng.shuffle(items)

    keys = sorted(buckets)
    picked: list[dict[str, Any]] = []
    source_counts: dict[str, int] = defaultdict(int)
    # Round-robin across buckets, respecting the per-source cap.
    while len(picked) < n:
        progressed = False
        for key in keys:
            items = buckets[key]
            while items:
                c = items.pop()
                slug = c.get("source_slug", "?")
                if source_counts[slug] >= per_source_cap:
                    continue
                picked.append(c)
                source_counts[slug] += 1
                progressed = True
                break
            if len(picked) >= n:
                break
        if not progressed:
            break
    return picked[:n]


def format_chunk(index: int, c: dict[str, Any], width: int = 1400) -> str:
    sig = c.get("signals", {})
    text = c.get("text", "")
    text = text[:width] + ("\n...[truncated]" if len(text) > width else "")
    return (
        f"\n{'='*100}\n"
        f"[{index}] {c.get('chunk_id')}  slug={c.get('source_slug')}  "
        f"url_class={c.get('url_class')}  hint={c.get('candidate_hint')}  "
        f"tokens={c.get('tokens')}\n"
        f"url: {c.get('url')}\n"
        f"title: {c.get('page_title')!r}   heading: {c.get('heading')!r}\n"
        f"signals: dates={sig.get('dates')} has_time={sig.get('has_time')} "
        f"event_terms={sig.get('event_terms')} news_terms={sig.get('news_terms')}\n"
        f"{'-'*100}\n{text}\n"
    )


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--processed", type=Path, default=DEFAULT_PROCESSED)
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--per-source-cap", type=int, default=3)
    p.add_argument("--out", type=Path, default=None,
                   help="write candidate chunks as JSONL instead of printing")
    p.add_argument("--url-class", default=None, help="restrict to one url_class")
    p.add_argument("--hint", default=None, help="restrict to one candidate_hint")
    p.add_argument("--include-dropped", action="store_true",
                   help="also sample date-filtered chunks (chunks_dropped.jsonl)")
    opts = p.parse_args(argv)

    chunks = load_all(opts.processed, include_dropped=opts.include_dropped)
    if opts.url_class:
        chunks = [c for c in chunks if c.get("url_class") == opts.url_class]
    if opts.hint:
        chunks = [c for c in chunks if c.get("candidate_hint") == opts.hint]
    sample = stratified_sample(
        chunks, opts.n, seed=opts.seed, per_source_cap=opts.per_source_cap
    )

    if opts.out:
        opts.out.parent.mkdir(parents=True, exist_ok=True)
        with opts.out.open("w", encoding="utf-8") as fh:
            for c in sample:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        meta = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "seed": opts.seed,
            "n_requested": opts.n,
            "n_written": len(sample),
            "per_source_cap": opts.per_source_cap,
            "url_class_filter": opts.url_class,
            "hint_filter": opts.hint,
            "include_dropped": opts.include_dropped,
            "corpus": corpus_snapshot(opts.processed),
        }
        meta_path = opts.out.with_suffix(opts.out.suffix + ".meta.json")
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {len(sample)} candidates -> {opts.out}")
        print(f"wrote sample manifest    -> {meta_path}")
    else:
        for i, c in enumerate(sample, 1):
            print(format_chunk(i, c))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
