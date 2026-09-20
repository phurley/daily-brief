#!/usr/bin/env python3
"""Benchmark extraction models: schema validity (1st try), cost, latency, quality.

Runs each model once per sampled chunk (no retries) and records:

    * whether the first-try JSON validates against our extraction schema;
    * actual OpenRouter cost (``usage.include``) and latency;
    * how many records parsed;
    * an optional LLM-judge quality score (1-5).

    .venv/bin/python -m extract.bench --per-mode 4
    .venv/bin/python -m extract.bench --per-mode 4 --judge-model openai/gpt-4o
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
from pathlib import Path
from typing import Any, Optional

from . import characterize, jev, llm, prompts, router

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = SCRIPT_DIR / "gold" / "round2"

DEFAULT_MODELS = [
    "google/gemini-2.5-flash",
    "deepseek/deepseek-chat",
    "meta-llama/llama-3.3-70b-instruct",
    "openai/gpt-4o-mini",
    "qwen/qwen-2.5-72b-instruct",
]

_JUDGE_SYSTEM = (
    "You grade a structured extraction against the source text for a local daily "
    "brief. Judge only: are the facts faithful to the text (no fabrication), and "
    "are the requested fields filled correctly? Respond with JSON."
)
_JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "fabrication": {"type": "boolean"},
        "key_field_ok": {
            "type": "boolean",
        },
        "note": {"type": "string"},
    },
    "required": ["score", "fabrication", "key_field_ok", "note"],
}


def _validator(schema: dict[str, Any]):
    import jsonschema

    return jsonschema.Draft202012Validator(schema)


def build_sample(candidates: list[dict[str, Any]], v7: dict[str, dict[str, Any]],
                 per_mode: int) -> list[tuple[dict[str, Any], jev.Triage, router.Route]]:
    by_mode: dict[str, list[tuple[dict[str, Any], jev.Triage, router.Route]]] = collections.defaultdict(list)
    for record in candidates:
        raw = v7.get(record.get("chunk_id", ""))
        if not raw:
            continue
        triage = jev.triage_from_raw(record.get("chunk_id", ""), raw)
        route = router.route(triage)
        if route.action == "extract":
            by_mode[route.mode].append((record, triage, route))
    sample = []
    for mode in sorted(by_mode):
        sample.extend(by_mode[mode][:per_mode])
    return sample


def run_bench(sample, models, *, limit_skip_errors: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in models:
        client = llm.ChatClient(model=model)
        print(f"\n=== {model} ===")
        for record, triage, route in sample:
            schema = prompts.schema_for(route.mode)
            messages = prompts.build_messages(route.mode, record, route.record_cap)
            result = client.complete_once(messages, schema, schema_name=prompts.schema_name(route.mode))
            valid = False
            if isinstance(result.get("data"), (dict, list)):
                valid = _validator(schema).is_valid(result["data"])
            records = prompts.records_from_response(route.mode, result["data"]) if result.get("data") else []
            usage = result.get("usage") or {}
            rows.append({
                "model": model,
                "chunk_id": record.get("chunk_id"),
                "mode": route.mode,
                "schema_valid": valid,
                "records": len(records),
                "cost": usage.get("cost"),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "latency": result.get("latency"),
                "error": result.get("error"),
                "response_format_used": result.get("response_format_used"),
                "data": result.get("data"),
                "extracted": records,
                "text": record.get("text", "")[:2000],
            })
            flag = "ok" if valid else ("err" if result.get("error") else "INVALID")
            print(f"  {route.mode:13s} {flag:7s} recs={len(records)} cost=${(usage.get('cost') or 0):.6f} {result.get('latency')}s")
    return rows


def judge(rows: list[dict[str, Any]], judge_model: str) -> None:
    client = llm.ChatClient(model=judge_model)
    print(f"\n=== judging with {judge_model} ===")
    for row in rows:
        if row.get("error") or not row.get("data"):
            continue
        user = (
            f"SOURCE TEXT:\n{row['text']}\n\n"
            f"EXTRACTED JSON:\n{json.dumps(row['data'], ensure_ascii=False)[:3000]}\n\n"
            "Grade the extraction. key_field_ok = is the most important key field "
            "correct and supported (the start date for an event, the publishedAt/headline "
            "for a story); false if it was guessed, wrong, or set to the literal string "
            "'null'. score 1-5 (5 = faithful and complete, 1 = fabricated/wrong)."
        )
        messages = [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": user}]
        from . import llm as _llm
        try:
            out = client.complete_json(messages, _JUDGE_SCHEMA, schema_name="judge")
            row["judge_score"] = out.get("score")
            row["judge_fabrication"] = out.get("fabrication")
            row["judge_key_ok"] = out.get("key_field_ok")
            row["judge_note"] = out.get("note")
        except _llm.LLMError as exc:
            row["judge_score"] = None
            row["judge_error"] = str(exc)[:120]


def summarise(rows: list[dict[str, Any]]) -> None:
    by_model: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        by_model[row["model"]].append(row)
    print(f"\n{'model':38s} {'n':>3s} {'schema%':>8s} {'avg_cost':>10s} {'cost/rec':>9s} {'p50_lat':>8s} {'judge':>6s} {'key_ok':>7s}")
    for model, items in by_model.items():
        n = len(items)
        valid = sum(1 for r in items if r["schema_valid"])
        cost = sum((r.get("cost") or 0) for r in items)
        recs = sum(r["records"] for r in items)
        lat = [r["latency"] for r in items if r.get("latency") is not None]
        scores = [r["judge_score"] for r in items if r.get("judge_score")]
        keyok = [r.get("judge_key_ok") for r in items if r.get("judge_key_ok") is not None]
        judge_avg = f"{statistics.mean(scores):.2f}" if scores else "-"
        key_rate = f"{sum(keyok)/len(keyok):.0%}" if keyok else "-"
        print(f"{model:38s} {n:3d} {valid/n:8.0%} {cost/n:10.6f} "
              f"{(cost/recs if recs else 0):9.6f} {statistics.median(lat):8.2f} "
              f"{judge_avg:>6s} {key_rate:>7s}")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    p.add_argument("--per-mode", type=int, default=4)
    p.add_argument("--models", nargs="*", default=DEFAULT_MODELS)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--out", type=Path, default=DEFAULT_GOLD / "model_bench.jsonl")
    p.add_argument("--dry-run", action="store_true")
    opts = p.parse_args(argv)

    candidates = characterize.load_jsonl(opts.gold / "candidates.jsonl")
    v7 = {r["chunk_id"]: r["answers"] for r in characterize.load_jsonl(opts.gold / "characterization_v8.jsonl")}
    sample = build_sample(candidates, v7, opts.per_mode)
    print(f"sample: {len(sample)} chunks across {len(set(r.mode for _, _, r in sample))} modes")
    if opts.dry_run:
        for _, triage, route in sample:
            print(f"  {route.mode:13s} card={triage.cardinality}")
        return 0

    rows = run_bench(sample, opts.models)
    if opts.judge_model:
        judge(rows, opts.judge_model)
    opts.out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"\nwrote {len(rows)} rows -> {opts.out}")
    summarise(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())