#!/usr/bin/env python3
"""Gate-question lab: test competing filter questions in one Jev pass.

Each candidate gets ONE decision call carrying every question variant, then
each variant is scored against two ground truths:

* hand labels — is this candidate a listicle/roundup (drop-worthy filler)?
* extraction outcomes — did this candidate's record pass publish validation
  (from the record store bucket it was sampled by: clean_event / news /
  rework / ics_control)?

Run:  .venv/bin/python gate_lab.py
"""
import json, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import pipeline as pl, jev

# ---- competing questions ----------------------------------------------------
VARIANTS = {
    "v1_listicle": (
        "Is this chunk a listicle — an article written as a numbered or "
        "curated list (e.g. '5 Ways to...', '10 Best...') whose items are "
        "too brief to have their own date, venue, or details?"
    ),
    "v2_roundup": (
        "Does this chunk round up multiple separate places, businesses, "
        "things, or events (a 'best of', 'things to do', or weekly digest) "
        "rather than covering one specific event or story in depth?"
    ),
    "v3_event_listing": (
        "Is this the event's own listing or detail page — does it state the "
        "event's date, time, and venue directly, as a calendar entry would?"
    ),
    "v4_article_about_event": (
        "Is this journalism ABOUT an event — a preview, review, feature, or "
        "announcement article — rather than the event's listing itself? "
        "(The event's date/venue may be mentioned only in passing.)"
    ),
    "v5_details_missing": (
        "Does this chunk mention one or more events but NOT state the date "
        "and venue needed to attend each one?"
    ),
    "v6_low_value_filler": (
        "Is this low-value filler for a local daily brief — a listicle, "
        "roundup, evergreen tips article, or generic promotional content — "
        "rather than a specific dated event or news story?"
    ),
}

LAB_TASK = {
    "name": "gate_lab",
    "version": "1",
    "description": "Candidate-gate filter experiments for the Daily Brief.",
    "fields": {
        name: {"type": "bool", "question": q,
               "true": "Yes", "false": "No"}
        for name, q in VARIANTS.items()
    },
}

# ---- labeled set -------------------------------------------------------------
sample = json.load(open('/tmp/listicle-eval-sample.json'))
regex_hits = json.load(open('/tmp/listicle-regex-hits.json'))
STORE_POS = {30, 38, 68, 69}          # listicles (podcast roundups re-labeled N)
REGEX_LISTICLE = ("openai", "top 10 shows", "10 things to do", "top 10 most challenged")

rows = {}
for i, r in enumerate(sample):
    rows[(r['src'], r['cid'])] = {
        'bucket': r['bucket'], 'listicle': i in STORE_POS,
        'head': r['title'][:70], 'src': r['src'],
    }
for r in regex_hits:
    key = (r['src'], r['cid'])
    if key in rows:
        continue
    t = r['title'].lower()
    rows[key] = {'bucket': 'regex', 'listicle': any(k in t for k in REGEX_LISTICLE),
                 'head': r['title'][:70], 'src': r['src']}

# rework = the record failed publish validation (the re-work population)
BAD_OUTCOME = {'rework'}

# ---- run one Jev call per candidate ----------------------------------------
cands = {}
for c in pl.load_candidates(Path('processed')):
    cands[(c.get('source_slug'), c.get('chunk_id') or c.get('item_id'))] = c
eval_set = [(k, v) for k, v in rows.items() if k in cands]
print(f"eval set: {len(eval_set)} candidates "
      f"({sum(1 for _, v in eval_set if v['listicle'])} hand-labeled listicles, "
      f"{sum(1 for _, v in eval_set if v['bucket'] in BAD_OUTCOME)} re-work outcomes)")

client = jev.make_client()

def run_one(item):
    (src, cid), meta = item
    try:
        raw = client.decide(LAB_TASK, jev.chunk_state(cands[(src, cid)]))
    except Exception as e:
        return {'key': (src, cid), **meta, 'error': f"{type(e).__name__}: {e}"}
    d = jev._decision_from_raw(cid, raw)
    out = {'key': (src, cid), **meta}
    for name in VARIANTS:
        f = d.fields.get(name)
        out[name] = (bool(f.value), f.confidence) if f else (False, 0.0)
    return out

with ThreadPoolExecutor(max_workers=6) as pool:
    results = [r for r in pool.map(run_one, eval_set) if r]
ok = [r for r in results if 'error' not in r]
print(f"ran {len(ok)} decisions ({len(results) - len(ok)} errors)\n")

# ---- scoring -----------------------------------------------------------------
def score(name, pred, target, label_fn):
    tp = sum(1 for r in ok if label_fn(r) == target and pred(r))
    fp = sum(1 for r in ok if label_fn(r) != target and pred(r))
    fn = sum(1 for r in ok if label_fn(r) == target and not pred(r))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    print(f"  {name:34s} tp={tp:3d} fp={fp:3d} fn={fn:3d}  P={prec:.2f} R={rec:.2f}")

print("== vs hand-labeled listicles (positives = listicles) ==")
for name in VARIANTS:
    score(name, lambda r, n=name: r[n][0], True, lambda r: r['listicle'])

print("\n== vs extraction outcome (positives = re-work records) ==")
for name in VARIANTS:
    score(name, lambda r, n=name: r[n][0], True,
          lambda r: r['bucket'] in BAD_OUTCOME)

# drop-rule simulation: flag & confidence >= threshold
print("\n== drop-rule simulation: variant flagged with confidence >= 0.60 ==")
for name in VARIANTS:
    def dropped(r, n=name):
        v, c = r[n]
        return v and c >= 0.60
    dropped_clean = sum(1 for r in ok if dropped(r) and r['bucket'] in ('clean_event', 'news', 'ics_control'))
    dropped_rework = sum(1 for r in ok if dropped(r) and r['bucket'] == 'rework')
    dropped_list = sum(1 for r in ok if dropped(r) and r['listicle'])
    n_rework = sum(1 for r in ok if r['bucket'] == 'rework')
    print(f"  {name:22s} drops {dropped_rework:2d}/{n_rework} re-work, "
          f"{dropped_list:2d} listicles, {dropped_clean:2d} GOOD (collateral)")

# route-rule simulation: v4 article-about-event -> route to news extraction
print("\n== route-rule simulation: v4 flagged AND bucket=rework (would become news) ==")
v4_hits = [r for r in ok if r['v4_article_about_event'][0] and r['bucket'] == 'rework']
v4_clean = [r for r in ok if r['v4_article_about_event'][0] and r['bucket'] == 'clean_event']
print(f"  re-work diverted to news: {len(v4_hits)}   clean events misrouted: {len(v4_clean)}")
for r in v4_clean[:6]:
    print(f"    MISROUTE conf={r['v4_article_about_event'][1]:.2f} {r['src'][:22]:22s} {r['head']}")

json.dump([{k: v for k, v in r.items() if k != 'key'} | {'src': r['key'][0], 'cid': r['key'][1]} for r in ok],
          open('/tmp/gate-lab-results.json', 'w'), indent=1, default=str)
print("\nwrote /tmp/gate-lab-results.json")
