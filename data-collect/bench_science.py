"""Science extraction bench: replay stored articles through candidate models.

Run from data-collect with LLM_CACHE=off and LLM_USAGE_LOG pointing at a scratch file:
  .venv/bin/python bench_science.py processed/science 30
Rows are written to /tmp/bench-science-rows.json.
Scores per model: valid JSON, truncations, relevant-agreement with the
production extraction, build_story pass rate (quote/paper verification),
judge quality 1-5 on accepted stories, latency, tokens.
"""
import glob, json, os, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from extract import llm
from science import pipeline as sp

STATE = Path(sys.argv[1]); N = int(sys.argv[2])
# "model@effort" sets reasoning_effort for that run, e.g. google/gemini-3.8-flash@low
MODELS = os.getenv("SCIENCE_BENCH_MODELS", "google/gemini-2.5-flash,google/gemini-3.1-flash-lite,google/gemini-3.8-flash@low").split(",")
JUDGE = "openai/gpt-4.1-mini"

config = json.loads((STATE.parents[1] / "science-sources.json").read_text())
titles = {}
for feed in glob.glob(str(STATE / "feeds" / "*.json")):
    for e in sp.parse_feed(json.loads(Path(feed).read_text())["xml"].encode()):
        titles[sp.canonical(e["url"])] = e
docs = [json.loads(Path(f).read_text()) for f in glob.glob(str(STATE / "extractions" / "*.json"))]
docs = [d for d in docs if sp.canonical(d["url"]) in titles]
docs.sort(key=lambda d: d["checkedAt"], reverse=True)
docs = docs[:N]
print(f"{len(docs)} articles with feed titles", flush=True)


def source_for(url):
    host = urlsplit(url).hostname or ""
    return next(s for s in config["sources"] if any(host == d or host.endswith("." + d) for d in s["articleDomains"]))


def run_one(model, doc):
    entry = titles[sp.canonical(doc["url"])]
    name, _, effort = model.partition("@")
    client = llm.ChatClient(model=name, timeout=120, retries=1, caller="bench_science:" + model, max_tokens=2500,
                            reasoning_effort=effort or None)
    messages = [{"role": "system", "content": sp.PROMPT},
                {"role": "user", "content": json.dumps({"title": entry["title"], "article": sp.prompt_article(doc["article"], entry["url"])})}]
    t = time.time(); row = {"model": model, "url": doc["url"], "base": doc["extraction"]}
    try:
        row["out"] = client.complete_json(messages, sp.EXTRACTION_SCHEMA, schema_name="science_story")
    except llm.TruncatedOutput:
        row["error"] = "truncated"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    row["latency"] = time.time() - t
    if "out" in row:
        try:
            story = sp.build_story(entry, doc["article"], json.loads(json.dumps(row["out"])), source_for(doc["url"]),
                                   "2026-11-06T12:00:00Z", "2026-11-06T12:00:00Z")
            row["verdict"] = "accepted" if story else "not-relevant"
        except Exception as exc:
            row["verdict"] = f"rejected: {str(exc)[:60]}"
    return row


def judge(row, doc):
    out = row["out"]
    prompt = ("Rate this science-news extraction against the article, 1-5 each: accuracy (finding/significance/caveat "
              "faithful to the article, no invented details), insight (significance explains why it matters), "
              "caveat (a real, material limitation). Reply JSON {\"accuracy\":n,\"insight\":n,\"caveat\":n}.\n\n"
              f"ARTICLE:\n{doc['article']['text'][:9000]}\n\nEXTRACTION:\n" +
              json.dumps({k: out.get(k) for k in ("finding", "significance", "caveat", "evidenceType")}))
    client = llm.ChatClient(model=JUDGE, retries=1, caller="bench_science_judge")
    try:
        return client.complete_json([{"role": "user", "content": prompt}])
    except Exception:
        return None


rows = []
with ThreadPoolExecutor(6) as pool:
    for model in MODELS:
        rows += list(pool.map(lambda d: run_one(model, d), docs))
        print(f"  {model} done", flush=True)
by_url = {d["url"]: d for d in docs}
accepted = [r for r in rows if r.get("verdict") == "accepted"]
with ThreadPoolExecutor(6) as pool:
    for r, score in zip(accepted, pool.map(lambda r: judge(r, by_url[r["url"]]), accepted)):
        r["judge"] = score
Path("/tmp/bench-science-rows.json").write_text(json.dumps(rows, indent=1))

usage_rows = [json.loads(l) for l in open(os.environ["LLM_USAGE_LOG"])] if os.path.exists(os.environ.get("LLM_USAGE_LOG", "")) else []
print(f"\n{'model':32s} json trunc err  rel=base accept reject  judge(acc/ins/cav)  p50s  out-tok/call")
for model in MODELS:
    rs = [r for r in rows if r["model"] == model]
    ok = [r for r in rs if "out" in r]
    agree = sum(r["out"]["relevant"] == r["base"]["relevant"] for r in ok)
    acc = [r for r in rs if r.get("verdict") == "accepted"]
    rej = [r for r in rs if str(r.get("verdict", "")).startswith("rejected")]
    js = [r["judge"] for r in acc if r.get("judge")]
    mean = lambda k: statistics.mean(j[k] for j in js) if js else 0
    toks = [u["completionTokens"] or 0 for u in usage_rows if u["caller"] == "bench_science:" + model and u["kind"] == "chat"]
    print(f"{model:32s} {len(ok):2d}/{len(rs)} {sum(r.get('error')=='truncated' for r in rs):4d} "
          f"{sum('error' in r and r['error']!='truncated' for r in rs):3d}  {agree:2d}/{len(ok):<3d}  {len(acc):5d} {len(rej):6d}  "
          f"{mean('accuracy'):.2f}/{mean('insight'):.2f}/{mean('caveat'):.2f}     "
          f"{statistics.median(r['latency'] for r in rs):5.1f}  {statistics.mean(toks) if toks else 0:8.0f}")
for model in MODELS:
    reasons = {}
    for r in rows:
        if r["model"] == model and str(r.get("verdict", "")).startswith("rejected"):
            reasons[r["verdict"]] = reasons.get(r["verdict"], 0) + 1
    print(model, "rejections:", reasons)
