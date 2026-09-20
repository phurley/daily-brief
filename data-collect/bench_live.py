#!/usr/bin/env python3
"""Live model bench: sample candidates from processed/, run the actual
extraction prompts through competing OpenRouter models, judge quality.

    .venv/bin/python -u bench_live.py --phase extract   # writes /tmp/model-bench-live.jsonl
    .venv/bin/python -u bench_live.py --phase judge     # reads rows, adds judge scores
    .venv/bin/python -u bench_live.py --phase report    # prints the table
"""
import argparse, collections, json, random, statistics, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import bench, jev, llm, pipeline as pl, prompts, router

MODELS = [
    "qwen/qwen3-32b",             # current default (baseline)
    "qwen/qwen3.5-flash-02-23",
    "qwen/qwen3.6-flash",
    "qwen/qwen3.7-flash",
    "qwen/qwen3.8-flash",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
    "google/gemini-3.1-flash-lite",
    "openai/gpt-4.1-mini",
    "openai/gpt-4o-mini",
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-v4-flash",
    "z-ai/glm-5.3-flash",
]
JUDGE_MODEL = "openai/gpt-4.1-mini"
ROWS_PATH = Path("/tmp/model-bench-live.jsonl")
SAMPLE_PATH = Path("/tmp/model-bench-sample.json")
CALL_TIMEOUT = 30.0


def build_live_sample(per_mode: int, seed: int = 7):
    cands = pl.load_candidates(Path("processed"))
    random.Random(seed).shuffle(cands)
    by_slug: dict[str, int] = collections.Counter()
    spread = []
    for c in cands:
        s = c.get("source_slug") or "?"
        if by_slug[s] < 2:
            spread.append(c)
            by_slug[s] += 1
    client = jev.make_client()
    by_mode: dict[str, list] = collections.defaultdict(list)

    def triage_one(c):
        return c, jev.triage_record(c, client)

    with ThreadPoolExecutor(max_workers=6) as pool:
        for c, tri in pool.map(triage_one, spread[:100]):
            route = router.route(tri)
            if route.action == "extract" and len(by_mode[route.mode]) < per_mode:
                by_mode[route.mode].append((c.get("chunk_id") or c.get("item_id"), route.mode, route.record_cap))
    sample = [{"cid": cid, "mode": mode, "cap": cap}
              for mode in sorted(by_mode) for cid, mode, cap in by_mode[mode]]
    return sample, cands


def run_model(model, sample, cands_by_id):
    client = llm.ChatClient(model=model, timeout=CALL_TIMEOUT)
    rows = []
    for s in sample:
        record = cands_by_id[s["cid"]]
        schema = prompts.schema_for(s["mode"])
        messages = prompts.build_messages(s["mode"], record, s["cap"])
        result = client.complete_once(messages, schema, schema_name=prompts.schema_name(s["mode"]))
        valid = False
        if isinstance(result.get("data"), (dict, list)):
            valid = bench._validator(schema).is_valid(result["data"])
        records = prompts.records_from_response(s["mode"], result["data"]) if result.get("data") else []
        usage = result.get("usage") or {}
        rows.append({
            "model": model, "mode": s["mode"], "cid": s["cid"],
            "schema_valid": valid, "records": len(records),
            "cost": usage.get("cost"), "latency": result.get("latency"),
            "error": result.get("error")[:120] if result.get("error") else None,
            "data": result.get("data"),
            "text": (record.get("text") or "\n".join(filter(None, (
                record.get("title"), record.get("summary"), record.get("venue")))))[:2000],
        })
    return rows


def phase_extract(per_mode, resume=False):
    if resume and SAMPLE_PATH.exists():
        sample = json.load(SAMPLE_PATH.open())
        cands = pl.load_candidates(Path("processed"))
        print(f"reusing saved sample: {len(sample)} chunks", flush=True)
    else:
        sample, cands = build_live_sample(per_mode)
        json.dump(sample, SAMPLE_PATH.open("w"))
        print(f"sample: {len(sample)} chunks -> {SAMPLE_PATH}", flush=True)
    cands_by_id = {c.get("chunk_id") or c.get("item_id"): c for c in cands}
    done_models = set()
    if resume and ROWS_PATH.exists():
        done_models = {json.loads(l)["model"] for l in ROWS_PATH.read_text().splitlines() if l.strip()}
        print(f"resuming; skipping {len(done_models)} done models", flush=True)
    mode = "a" if resume and ROWS_PATH.exists() else "w"
    with ROWS_PATH.open(mode) as fh:
        for model in MODELS:  # sequential per model keeps one row-block intact
            if model in done_models:
                continue
            rows = run_model(model, sample, cands_by_id)
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")
            fh.flush()
            ok = sum(1 for r in rows if r["schema_valid"])
            err = sum(1 for r in rows if r["error"])
            cost = sum((r.get("cost") or 0) for r in rows)
            print(f"  {model:32s} schema={ok}/{len(rows)} errors={err} cost=${cost:.4f}", flush=True)


def phase_judge():
    rows = [json.loads(l) for l in ROWS_PATH.read_text().splitlines() if l.strip()]
    todo = [i for i, r in enumerate(rows) if r.get("data") and not r.get("judge_score")]
    print(f"judging {len(todo)}/{len(rows)} rows with {JUDGE_MODEL}", flush=True)
    client = llm.ChatClient(model=JUDGE_MODEL, timeout=CALL_TIMEOUT)

    def judge_one(i):
        row = rows[i]
        user = (
            f"SOURCE TEXT:\n{row['text']}\n\n"
            f"EXTRACTED JSON:\n{json.dumps(row['data'], ensure_ascii=False)[:3000]}\n\n"
            "Grade the extraction. key_field_ok = is the most important key field "
            "correct and supported (the start date for an event, the publishedAt/headline "
            "for a story); false if it was guessed, wrong, or set to the literal string "
            "'null'. score 1-5 (5 = faithful and complete, 1 = fabricated/wrong)."
        )
        try:
            out = client.complete_json(
                [{"role": "system", "content": bench._JUDGE_SYSTEM},
                 {"role": "user", "content": user}], bench._JUDGE_SCHEMA, schema_name="judge")
            row["judge_score"] = out.get("score")
            row["judge_fabrication"] = out.get("fabrication")
            row["judge_key_ok"] = out.get("key_field_ok")
        except llm.LLMError as exc:
            row["judge_error"] = str(exc)[:120]
        return i, row

    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        with ROWS_PATH.open("w") as fh:
            written = 0
            for i, row in pool.map(judge_one, todo):
                rows[i] = row
                done += 1
                if done % 20 == 0:
                    print(f"  judged {done}/{len(todo)}", flush=True)
            for r in rows:
                fh.write(json.dumps(r, default=str) + "\n")
    print(f"judged {done} rows", flush=True)


def phase_report():
    rows = [json.loads(l) for l in ROWS_PATH.read_text().splitlines() if l.strip()]
    print(f"\n{'model':30s} {'sch%':>4s} {'errs':>4s} {'recs':>4s} {'$call':>8s} {'p50s':>5s} {'judge':>5s} {'keyok':>5s} {'fab':>4s}")
    for model in MODELS:
        items = [r for r in rows if r["model"] == model]
        if not items:
            print(f"{model:30s} (no rows)"); continue
        n = len(items)
        valid = sum(1 for r in items if r["schema_valid"])
        errs = sum(1 for r in items if r["error"])
        recs = sum(r["records"] for r in items)
        cost = sum((r.get("cost") or 0) for r in items)
        lat = sorted(r["latency"] for r in items if r.get("latency") is not None)
        scores = [r["judge_score"] for r in items if r.get("judge_score")]
        keyok = [r["judge_key_ok"] for r in items if r.get("judge_key_ok") is not None]
        fab = [r["judge_fabrication"] for r in items if r.get("judge_fabrication") is not None]
        print(f"{model:30s} {valid/n:4.0%} {errs:4d} {recs:4d} {cost/n:8.5f} "
              f"{(statistics.median(lat) if lat else 0):5.1f} "
              f"{(statistics.mean(scores) if scores else 0):5.2f} "
              f"{(sum(keyok)/len(keyok) if keyok else 0):5.0%} "
              f"{(sum(1 for x in fab if x)/len(fab) if fab else 0):4.0%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["extract", "judge", "report", "all"], default="all")
    ap.add_argument("--per-mode", type=int, default=3)
    ap.add_argument("--resume", action="store_true", help="extract: skip models already in the rows file")
    opts = ap.parse_args()
    if opts.phase in ("extract", "all"):
        phase_extract(opts.per_mode, resume=opts.resume)
    if opts.phase in ("judge", "all"):
        phase_judge()
    phase_report()


if __name__ == "__main__":
    main()
