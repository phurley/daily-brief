#!/usr/bin/env python3
"""Weekly model quality check on frozen samples, so scores are comparable.

Preview models (gemini-3.1-flash-lite-preview) can change without notice and a
silent quality drop costs more than any price change. This runs each role's
current primary model (from models.json) over the same frozen inputs every
week, judges the answers with an independent model, and appends one row per
role to processed/model-quality.jsonl. The health report flags drops.

    cd data-collect && .venv/bin/python quality_canary.py   # run both roles
    .venv/bin/python quality_canary.py --role science
    .venv/bin/python quality_canary.py --refreeze      # pick new samples first

Samples live in processed/quality-canary/. They are frozen on first run: the
extraction sample stores the exact prompts, the science sample the stored
articles. Refreezing resets the baseline, so compare only rows with the same
``sample`` id. Costs about $0.10 a run.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from extract import env, llm, models  # noqa: E402

PROCESSED = HERE / "processed"
SAMPLES = PROCESSED / "quality-canary"
LOG = PROCESSED / "model-quality.jsonl"
JUDGE_MODEL = "openai/gpt-4.1-mini"  # a different vendor from the models it grades
SCIENCE_ARTICLES = 20
# Flag a drop when a score falls this far below the median of earlier runs.
JUDGE_DROP = 0.3
RATE_DROP = 0.10


def _sample_id(rows) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]


def freeze_extract(per_mode: int = 3) -> list[dict]:
    import bench_live
    from extract import pipeline as pl, prompts
    sample, cands = bench_live.build_live_sample(per_mode)
    by_id = {c.get("chunk_id") or c.get("item_id"): c for c in cands}
    rows = []
    for s in sample:
        record = by_id[s["cid"]]
        rows.append({"cid": s["cid"], "mode": s["mode"], "cap": s["cap"],
                     "messages": prompts.build_messages(s["mode"], record, s["cap"]),
                     "text": (record.get("text") or "\n".join(filter(None, (
                         record.get("title"), record.get("summary"), record.get("venue")))))[:2000]})
    return rows


def freeze_science(n: int = SCIENCE_ARTICLES) -> list[dict]:
    from science import pipeline as sp
    state = PROCESSED / "science"
    titles = {}
    for feed in glob.glob(str(state / "feeds" / "*.json")):
        for entry in sp.parse_feed(json.loads(Path(feed).read_text())["xml"].encode()):
            titles[sp.canonical(entry["url"])] = entry
    docs = [json.loads(Path(f).read_text()) for f in glob.glob(str(state / "extractions" / "*.json"))]
    docs = sorted((d for d in docs if sp.canonical(d["url"]) in titles), key=lambda d: d["checkedAt"], reverse=True)
    return [{"entry": titles[sp.canonical(d["url"])], "article": d["article"]} for d in docs[:n]]


def load_sample(role: str, refreeze: bool) -> list[dict]:
    path = SAMPLES / f"{role}-sample.json"
    if refreeze or not path.exists():
        rows = freeze_extract() if role == "extract" else freeze_science()
        if not rows:
            raise SystemExit(f"{role}: nothing to sample yet")
        SAMPLES.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False))
        print(f"{role}: froze {len(rows)} items -> {path}", flush=True)
    return json.loads(path.read_text())


def _judge(prompt: str, schema: dict, system: str | None = None) -> dict | None:
    client = llm.ChatClient(model=JUDGE_MODEL, retries=1, caller="quality_canary:judge")
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    try:
        return client.complete_json(messages, schema, schema_name="grade")
    except llm.LLMError:
        return None


def run_extract(sample: list[dict]) -> dict:
    from extract import bench, prompts
    model = models.primary("extract")
    client = llm.ChatClient(model=model, timeout=60, retries=1, caller="quality_canary:extract")

    def one(item):
        # The production path (complete_json): Gemini tier ladder, retries, deadline.
        schema = prompts.schema_for(item["mode"])
        started = time.time()
        try:
            data = client.complete_json(item["messages"], schema, schema_name=prompts.schema_name(item["mode"]))
            row = {"valid": bench._validator(schema).is_valid(data), "error": False}
        except llm.LLMError:
            data, row = None, {"valid": False, "error": True}
        row["latency"] = time.time() - started
        if data:
            grade = _judge(f"SOURCE TEXT:\n{item['text']}\n\nEXTRACTED JSON:\n"
                           f"{json.dumps(data, ensure_ascii=False)[:3000]}\n\nGrade the extraction. key_field_ok = "
                           "is the most important key field correct and supported (the start date for an event, "
                           "the publishedAt/headline for a story). score 1-5 (5 = faithful and complete, "
                           "1 = fabricated/wrong).", bench._JUDGE_SCHEMA, bench._JUDGE_SYSTEM)
            if grade:
                row.update(score=grade.get("score"), fabrication=grade.get("fabrication"),
                           key_ok=grade.get("key_field_ok"))
        return row

    with ThreadPoolExecutor(4) as pool:
        rows = list(pool.map(one, sample))
    scored = [r for r in rows if r.get("score")]
    return {"role": "extract", "model": model, "n": len(rows),
            "valid_rate": sum(r["valid"] for r in rows) / len(rows),
            "error_rate": sum(r["error"] for r in rows) / len(rows),
            "judge": statistics.mean(r["score"] for r in scored) if scored else None,
            "key_ok_rate": (sum(bool(r.get("key_ok")) for r in scored) / len(scored)) if scored else None,
            "fabrication_rate": (sum(bool(r.get("fabrication")) for r in scored) / len(scored)) if scored else None,
            "p50_s": statistics.median(r["latency"] for r in rows)}


SCIENCE_GRADE = {"type": "object", "additionalProperties": False,
                 "required": ["accuracy", "insight", "caveat"],
                 "properties": {k: {"type": "integer", "minimum": 1, "maximum": 5}
                                for k in ("accuracy", "insight", "caveat")}}


def run_science(sample: list[dict]) -> dict:
    from science import pipeline as sp
    config = json.loads((HERE / "science-sources.json").read_text())
    entry_spec = models.chain("science")[0]
    model = entry_spec["model"]
    client = llm.ChatClient(model=model, timeout=60, retries=1, caller="quality_canary:science",
                            max_tokens=models.max_tokens("science"), reasoning_effort=entry_spec.get("reasoning_effort"))

    def source_for(url):
        host = urlsplit(url).hostname or ""
        return next((s for s in config["sources"] if any(host == d or host.endswith("." + d) for d in s["articleDomains"])),
                    {"name": host, "group": host})

    def one(item):
        entry, article = item["entry"], item["article"]
        messages = [{"role": "system", "content": sp.PROMPT},
                    {"role": "user", "content": json.dumps({"title": entry["title"],
                                                            "article": sp.prompt_article(article, entry["url"])})}]
        started, row = time.time(), {}
        try:
            out = client.complete_json(messages, sp.EXTRACTION_SCHEMA, schema_name="science_story")
        except llm.TruncatedOutput:
            return {"verdict": "truncated", "latency": time.time() - started}
        except llm.LLMError:
            return {"verdict": "error", "latency": time.time() - started}
        row["latency"] = time.time() - started
        try:
            story = sp.build_story(entry, article, json.loads(json.dumps(out)), source_for(entry["url"]),
                                   "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")
            row["verdict"] = "accepted" if story else "not-relevant"
        except Exception:
            row["verdict"] = "rejected"
        if row["verdict"] == "accepted":
            grade = _judge("Rate this science-news extraction against the article, 1-5 each: accuracy (faithful, "
                           "no invented details), insight (significance explains why it matters), caveat (a real, "
                           f"material limitation).\n\nARTICLE:\n{article['text'][:9000]}\n\nEXTRACTION:\n" +
                           json.dumps({k: out.get(k) for k in ("finding", "significance", "caveat")}), SCIENCE_GRADE)
            if grade:
                row.update(grade)
        return row

    with ThreadPoolExecutor(4) as pool:
        rows = list(pool.map(one, sample))
    graded = [r for r in rows if "accuracy" in r]
    count = lambda verdict: sum(r["verdict"] == verdict for r in rows)
    mean = lambda key: statistics.mean(r[key] for r in graded) if graded else None
    return {"role": "science", "model": model, "n": len(rows),
            "accepted_rate": count("accepted") / len(rows), "rejected_rate": count("rejected") / len(rows),
            "truncated_rate": count("truncated") / len(rows), "error_rate": count("error") / len(rows),
            "judge": (statistics.mean((r["accuracy"] + r["insight"] + r["caveat"]) / 3 for r in graded)
                      if graded else None),
            "accuracy": mean("accuracy"), "insight": mean("insight"), "caveat": mean("caveat"),
            "p50_s": statistics.median(r["latency"] for r in rows)}


def history(role: str, sample: str, path: Path = LOG) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("role") == role and r.get("sample") == sample]


def regressions(row: dict, earlier: list[dict]) -> list[str]:
    """Drops against the median of earlier runs of the same model and sample."""
    same = [r for r in earlier if r["model"] == row["model"]]
    if not same:
        return []
    found = []
    base_judge = [r["judge"] for r in same if r.get("judge") is not None]
    if row.get("judge") is not None and base_judge and row["judge"] < statistics.median(base_judge) - JUDGE_DROP:
        found.append(f"judge {row['judge']:.2f} vs median {statistics.median(base_judge):.2f}")
    rate = "valid_rate" if row["role"] == "extract" else "accepted_rate"
    base_rate = [r[rate] for r in same if r.get(rate) is not None]
    if base_rate and row[rate] < statistics.median(base_rate) - RATE_DROP:
        found.append(f"{rate} {row[rate]:.0%} vs median {statistics.median(base_rate):.0%}")
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--role", choices=["extract", "science", "all"], default="all")
    ap.add_argument("--refreeze", action="store_true")
    opts = ap.parse_args(argv)
    env.load_dotenv()
    status = 0
    for role in (["extract", "science"] if opts.role == "all" else [opts.role]):
        sample = load_sample(role, opts.refreeze)
        row = run_extract(sample) if role == "extract" else run_science(sample)
        row = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sample": _sample_id(sample), **row}
        row["regressions"] = regressions(row, history(role, row["sample"]))
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
        print(json.dumps(row), flush=True)
        status |= bool(row["regressions"])
    return status


if __name__ == "__main__":
    raise SystemExit(main())
