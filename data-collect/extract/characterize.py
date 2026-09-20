#!/usr/bin/env python3
"""Characterization experiments for the extraction router.

Asks a combined set of Jev questions about each chunk once, caches the answers,
then scores several candidate taxonomies against the gold labels:

    * list / no-list
    * list / item / other          (fast filtering)
    * news yes/no
    * event yes/no

Usage::

    python3 -m extract.characterize --run          # live, cache to gold/round2
    python3 -m extract.characterize --score        # score cached answers
    python3 -m extract.characterize --run --limit 20 --client stub
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any, Optional

from . import jev, scoring

SCRIPT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = SCRIPT_DIR / "gold" / "round2"
DEFAULT_CACHE = DEFAULT_GOLD / "characterization.jsonl"

#: Combined task: every question is evaluated in parallel against one state.
CHARACTERIZATION: dict[str, Any] = {
    "name": "characterization",
    "version": "1",
    "description": "Characterize a chunk for extraction routing.",
    "fields": {
        "is_list": {
            "type": "bool",
            "question": (
                "Is this chunk a list, index, calendar, or search-results page "
                "that contains multiple separate events or news items, rather "
                "than a single item?"
            ),
            "true": "A list/index of several separate items",
            "false": "A single item, or not a list of items",
        },
        "is_single_event": {
            "type": "bool",
            "question": "Is this chunk a single, specific dated event or happening?",
            "true": "One identifiable event with a date",
            "false": "Not one specific event",
        },
        "is_single_news": {
            "type": "bool",
            "question": "Is this chunk a single news story or article?",
            "true": "One news story/article",
            "false": "Not one news story",
        },
        "cardinality": {
            "type": "enum",
            "values": ["none", "single", "few", "list"],
            "labels": {
                "none": "No extractable item",
                "single": "Exactly one item",
                "few": "Two to five items",
                "list": "Six or more items",
            },
            "question": "How many separate events/news items does this chunk contain?",
        },
        "schema": {
            "type": "enum",
            "values": ["event", "news", "notice", "mixed", "none"],
            "labels": {
                "event": "Dated events/happenings",
                "news": "News stories or articles",
                "notice": "Official/public notices",
                "mixed": "A mix of the above",
                "none": "None of these",
            },
            "question": "If extracted, which output schema fits this chunk?",
        },
    },
}


#: V2: structural list detection + a single primary-kind choice, instead of
#: separate single-event/single-news yes/no questions.
CHARACTERIZATION_V2: dict[str, Any] = {
    "name": "characterization_v2",
    "version": "1",
    "description": "Characterize a chunk for extraction routing (v2).",
    "fields": {
        "has_extractable": {
            "type": "bool",
            "question": (
                "Does this chunk contain at least one specific event, news story, "
                "or public notice that could become a structured record?"
            ),
            "true": "At least one extractable item",
            "false": "Nothing extractable",
        },
        "item_count": {
            "type": "enum",
            "values": ["none", "one", "two_to_five", "many"],
            "labels": {
                "none": "No extractable items",
                "one": "Exactly one item",
                "two_to_five": "Two to five items",
                "many": "Six or more items",
            },
            "question": "How many separate extractable items does this chunk contain?",
        },
        "is_index_page": {
            "type": "bool",
            "question": (
                "Is this chunk structurally a calendar, index, category listing, "
                "or search-results page rather than a single article or event page?"
            ),
            "true": "A calendar/index/category/search-results page",
            "false": "A single article, event, or non-listing page",
        },
        "primary_kind": {
            "type": "enum",
            "values": ["event", "news", "notice", "mixed", "none"],
            "labels": {
                "event": "Dated events/happenings",
                "news": "News stories or articles",
                "notice": "Official/public notices",
                "mixed": "A mix of the above",
                "none": "None of these",
            },
            "question": "What is the dominant kind of the extractable items?",
        },
    },
}

#: V3: cardinality + whether there is an *attendable* dated event. The idea is
#: that "could I attend this on a date" separates events from news about events,
#: policy, and calendars-of-nothing better than "is this a single event".
CHARACTERIZATION_V3: dict[str, Any] = {
    "name": "characterization_v3",
    "version": "1",
    "description": "Characterize a chunk: cardinality + attendability.",
    "fields": {
        "cardinality": {
            "type": "enum",
            "values": ["none", "single", "few", "list"],
            "labels": {
                "none": "No extractable item",
                "single": "Exactly one item",
                "few": "Two to five items",
                "list": "Six or more items",
            },
            "question": "How many separate extractable items does this chunk contain?",
        },
        "attend_on_date": {
            "type": "bool",
            "question": (
                "Does this chunk describe one or more events that a person could "
                "attend or participate in on a specific date (a concert, meeting, "
                "class, festival, game, tour, or similar)?"
            ),
            "true": "At least one attendable dated event",
            "false": "No attendable dated event (news, policy, reference, or no event)",
        },
        "has_specific_date": {
            "type": "bool",
            "question": (
                "Does the chunk give a specific calendar date or time (not just a "
                "season, a month name, or relative wording like 'recently')?"
            ),
            "true": "A specific date/time is present",
            "false": "No specific date/time",
        },
    },
}

#: V4 = the stable v1 field set plus attendability. Tests whether one call can
#: carry both cardinality (stable) and attend_on_date without interaction.
CHARACTERIZATION_V4: dict[str, Any] = {
    "name": "characterization_v4",
    "version": "1",
    "description": "v1 fields plus attendability.",
    "fields": {
        **CHARACTERIZATION["fields"],
        "attend_on_date": {
            "type": "bool",
            "question": (
                "Does this chunk describe one or more events that a person could "
                "attend or participate in on a specific date (a concert, meeting, "
                "class, festival, game, tour, or similar)?"
            ),
            "true": "At least one attendable dated event",
            "false": "No attendable dated event (news, policy, reference, or no event)",
        },
    },
}

#: V5 = v1 fields + attend + relevance: everything the router needs in one call.
CHARACTERIZATION_V5: dict[str, Any] = {
    "name": "characterization_v5",
    "version": "1",
    "description": "v1 fields + attendability + relevance.",
    "fields": {
        **CHARACTERIZATION["fields"],
        "attend_on_date": CHARACTERIZATION_V4["fields"]["attend_on_date"],
        "relevance": {
            "type": "int",
            "minimum": 0,
            "maximum": 4,
            "labels": [
                "Not relevant to a local daily brief",
                "Slightly relevant",
                "Moderately relevant",
                "Very relevant",
                "Essential to a local daily brief",
            ],
            "question": "How relevant is this to a local daily brief?",
        },
    },
}

#: V6 = v5 + is_content: the full one-call task if it stays stable.
CHARACTERIZATION_V6: dict[str, Any] = {
    "name": "characterization_v6",
    "version": "1",
    "description": "v5 fields + is_content.",
    "fields": {
        "is_content": {
            "type": "bool",
            "question": (
                "Is this substantive content (news, a dated event, a public "
                "notice) rather than navigation, chrome, a policy page, or boilerplate?"
            ),
            "true": "Substantive, brief-worthy content",
            "false": "Navigation, chrome, boilerplate, or reference material",
        },
        **CHARACTERIZATION_V5["fields"],
    },
}

#: V7 = v6 + kind: the full one-call task, including the gate's kind filter.
CHARACTERIZATION_V7: dict[str, Any] = {
    "name": "characterization_v7",
    "version": "1",
    "description": "v6 fields + kind.",
    "fields": {
        "is_content": CHARACTERIZATION_V6["fields"]["is_content"],
        "kind": {
            "type": "enum",
            "values": ["event", "news", "notice", "agenda", "listing", "nav", "other"],
            "labels": {
                "event": "A specific dated happening (concert, meeting, festival, class, game)",
                "news": "Reported news, article, or announcement",
                "notice": "Official or public notice",
                "agenda": "A meeting agenda or minutes",
                "listing": "An index page listing many items",
                "nav": "Site navigation, menu, header, footer, or other chrome",
                "other": "None of the above",
            },
            "question": "What kind of content is this chunk?",
        },
        **{k: v for k, v in CHARACTERIZATION_V5["fields"].items()},
    },
}

#: V8 = v7 fields with an explicit 0/1/2/3+ item count instead of
#: none/single/few/list. Single items were being over-labeled "few/list".
_ITEM_COUNT_FIELD = {
    "type": "enum",
    "values": ["0", "1", "2", "3+"],
    "labels": {
        "0": "No extractable event or news story",
        "1": "Exactly one event or news story",
        "2": "Exactly two separate events or news stories",
        "3+": "Three or more separate events or news stories",
    },
    "question": (
        "Count the separate events or news stories this chunk is about: 0, 1, 2, "
        "or 3 or more. Count ONLY the chunk's main content. Ignore site "
        "navigation, menus, headers, footers, sidebars, cookie/consent banners, "
        "ads, and 'related' / 'recommended' / 'more stories' link widgets. A "
        "single article or event page is 1 even if it mentions other events. "
        "Only count 2 or more when the main content itself is a list, calendar, "
        "index, or digest of that many distinct items."
    ),
}

CHARACTERIZATION_V8: dict[str, Any] = {
    "name": "characterization_v8",
    "version": "1",
    "description": "v7 fields with an explicit 0/1/2/3+ item count.",
    "fields": {
        "is_content": CHARACTERIZATION_V6["fields"]["is_content"],
        "kind": CHARACTERIZATION_V7["fields"]["kind"],
        "is_list": CHARACTERIZATION["fields"]["is_list"],
        "is_single_event": CHARACTERIZATION["fields"]["is_single_event"],
        "is_single_news": CHARACTERIZATION["fields"]["is_single_news"],
        "item_count": _ITEM_COUNT_FIELD,
        "schema": CHARACTERIZATION["fields"]["schema"],
        "attend_on_date": CHARACTERIZATION_V4["fields"]["attend_on_date"],
        "relevance": CHARACTERIZATION_V5["fields"]["relevance"],
    },
}

#: V9 = the full production triage field set + the 13 scoring Nouls, so events
#: arrive already scored from a single Jev call.
CHARACTERIZATION_V9: dict[str, Any] = {
    "name": "characterization_v9",
    "version": "1",
    "description": "v7 fields (item_count) + event scoring questions.",
    "fields": {
        **CHARACTERIZATION_V8["fields"],
        **scoring.SCORING_TASK["fields"],
    },
}

#: V10 = v9 with the retuned rubric (local removed, large_venue negative,
#: distinctive/eclectic/technical boosted). Separate cache so v9 is preserved.
CHARACTERIZATION_V10: dict[str, Any] = {
    "name": "characterization_v10",
    "version": "1",
    "description": "v8 fields + retuned event scoring questions.",
    "fields": {
        **CHARACTERIZATION_V8["fields"],
        **scoring.SCORING_TASK["fields"],
    },
}

TASKS = {
    "v1": CHARACTERIZATION,
    "v2": CHARACTERIZATION_V2,
    "v3": CHARACTERIZATION_V3,
    "v4": CHARACTERIZATION_V4,
    "v5": CHARACTERIZATION_V5,
    "v6": CHARACTERIZATION_V6,
    "v7": CHARACTERIZATION_V7,
    "v8": CHARACTERIZATION_V8,
    "v9": CHARACTERIZATION_V9,
    "v10": CHARACTERIZATION_V10,
}

DEFAULT_CACHES = {
    "v1": DEFAULT_GOLD / "characterization.jsonl",
    "v2": DEFAULT_GOLD / "characterization_v2.jsonl",
    "v3": DEFAULT_GOLD / "characterization_v3.jsonl",
    "v4": DEFAULT_GOLD / "characterization_v4.jsonl",
    "v5": DEFAULT_GOLD / "characterization_v5.jsonl",
    "v6": DEFAULT_GOLD / "characterization_v6.jsonl",
    "v7": DEFAULT_GOLD / "characterization_v7.jsonl",
    "v8": DEFAULT_GOLD / "characterization_v8.jsonl",
    "v9": DEFAULT_GOLD / "characterization_v9.jsonl",
    "v10": DEFAULT_GOLD / "characterization_v10.jsonl",
}


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def run(candidates: list[dict[str, Any]], client: jev.JevClient,
        task: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    task = task or CHARACTERIZATION
    out: list[dict[str, Any]] = []
    for record in candidates:
        answers = client.decide(task, jev.chunk_state(record))
        out.append({"chunk_id": record.get("chunk_id", ""), "answers": answers})
    return out


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Gold + scoring
# --------------------------------------------------------------------------- #

def gold_bucket(label: dict[str, Any]) -> str:
    records = int(label.get("records") or 0)
    if records >= 2:
        return "list"
    if records == 1 and label.get("target") == "event":
        return "item_event"
    if records == 1 and label.get("target") == "news":
        return "item_news"
    return "other"


def _binary(pred: list[bool], gold: list[bool]) -> dict[str, Any]:
    tp = sum(p and g for p, g in zip(pred, gold))
    fp = sum(p and not g for p, g in zip(pred, gold))
    fn = sum((not p) and g for p, g in zip(pred, gold))
    tn = sum((not p) and not g for p, g in zip(pred, gold))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 3), "recall": round(recall, 3)}


def _yes(answer: dict[str, Any]) -> bool:
    return bool(answer.get("value"))


def score(candidates: list[dict[str, Any]], answers: list[dict[str, Any]],
          labels: list[dict[str, Any]]) -> None:
    by_id = {a["chunk_id"]: a["answers"] for a in answers}
    rows = []
    for rec, lab in zip(candidates, labels):
        ans = by_id.get(rec.get("chunk_id", ""), {})
        gold = gold_bucket(lab["label"] if "label" in lab else lab)
        rows.append((rec, gold, ans))

    def field(ans, name):
        return ans.get(name, {})

    print(f"\n=== characterization: {len(rows)} chunks ===")

    # 1. list / no-list
    pred = [_yes(field(a, "is_list")) or field(a, "cardinality").get("value") in ("few", "list")
            for _, _, a in rows]
    gold = [g == "list" for _, g, _ in rows]
    print("list/no-list (pred = is_list || cardinality in few/list):",
          _binary(pred, gold))

    # 2. list / item / other
    tri_pred, tri_gold = [], []
    for _, g, a in rows:
        if _yes(field(a, "is_list")) or field(a, "cardinality").get("value") in ("few", "list"):
            tri_pred.append("list")
        elif _yes(field(a, "is_single_event")) or _yes(field(a, "is_single_news")) \
                or field(a, "cardinality").get("value") == "single":
            tri_pred.append("item")
        else:
            tri_pred.append("other")
        tri_gold.append("list" if g == "list" else ("item" if g.startswith("item") else "other"))
    cm = collections.Counter(zip(tri_gold, tri_pred))
    correct = sum(v for (g, p), v in cm.items() if g == p)
    print(f"\nlist/item/other accuracy={correct/len(rows):.2f}")
    print("  confusion (gold -> pred):")
    for g in ("list", "item", "other"):
        dist = {p: cm.get((g, p), 0) for p in ("list", "item", "other")}
        print(f"    {g:6s} {dist}")
    for cat in ("list", "item", "other"):
        tp = cm.get((cat, cat), 0)
        fp = sum(cm.get((g, cat), 0) for g in ("list", "item", "other") if g != cat)
        fn = sum(cm.get((cat, p), 0) for p in ("list", "item", "other") if p != cat)
        p = tp / (tp + fp) if tp + fp else 0
        r = tp / (tp + fn) if tp + fn else 0
        print(f"    {cat:6s} precision={p:.2f} recall={r:.2f}")

    # 3. news yes/no, 4. event yes/no (single items only)
    for name, pred_field, gold_flag in (
        ("news", "is_single_news", "item_news"),
        ("event", "is_single_event", "item_event"),
    ):
        p = [_yes(field(a, pred_field)) for _, _, a in rows]
        g = [gg == gold_flag for _, gg, _ in rows]
        print(f"\n{name} yes/no (pred = {pred_field}):", _binary(p, g))

    # raw cardinality vs gold 4-class
    card_gold = [gg for _, gg, _ in rows]
    card_pred = [field(a, "cardinality").get("value", "none") for _, _, a in rows]
    # map few->list for comparison? show as-is
    cm2 = collections.Counter(zip(card_gold, card_pred))
    print("\ncardinality choice vs gold (raw):")
    for g in ("list", "item_event", "item_news", "other"):
        print(f"    {g:11s}", {p: cm2.get((g, p), 0) for p in ("none", "single", "few", "list")})

    # extraction volume for the fast filter
    send = sum(1 for p in tri_pred if p in ("item", "list"))
    print(f"\nfast filter: send {send}/{len(rows)} to extraction, "
          f"drop {len(rows)-send} ({(len(rows)-send)/len(rows):.0%})")


def score_v2(candidates: list[dict[str, Any]], answers: list[dict[str, Any]],
             labels: list[dict[str, Any]]) -> None:
    by_id = {a["chunk_id"]: a["answers"] for a in answers}
    rows = []
    for rec, lab in zip(candidates, labels):
        ans = by_id.get(rec.get("chunk_id", ""), {})
        rows.append((rec, gold_bucket(lab["label"] if "label" in lab else lab), ans))

    print(f"\n=== characterization v2: {len(rows)} chunks ===")

    def yes(a, n):
        return bool(a.get(n, {}).get("value"))

    pred = [yes(a, "has_extractable") for _, _, a in rows]
    gold = [g != "other" for _, g, _ in rows]
    print("record detection (has_extractable):", _binary(pred, gold))

    pred = [yes(a, "is_index_page") or a.get("item_count", {}).get("value") in ("two_to_five", "many")
            for _, _, a in rows]
    gold = [g == "list" for _, g, _ in rows]
    print("list detection (is_index_page || count>=2):", _binary(pred, gold))

    # kind of single items
    ev_p = [yes(a, "has_extractable") and a.get("primary_kind", {}).get("value") == "event" for _, _, a in rows]
    ev_g = [g == "item_event" for _, g, _ in rows]
    nw_p = [yes(a, "has_extractable") and a.get("primary_kind", {}).get("value") == "news" for _, _, a in rows]
    nw_g = [g == "item_news" for _, g, _ in rows]
    print("event kind:", _binary(ev_p, ev_g))
    print("news kind:", _binary(nw_p, nw_g))

    cm = collections.Counter((g, a.get("item_count", {}).get("value", "none")) for _, g, a in rows)
    print("\nitem_count vs gold:")
    for g in ("list", "item_event", "item_news", "other"):
        print(f"    {g:11s}", {p: cm.get((g, p), 0) for p in ("none", "one", "two_to_five", "many")})


def score_v3(candidates: list[dict[str, Any]], answers: list[dict[str, Any]],
             labels: list[dict[str, Any]]) -> None:
    by_id = {a["chunk_id"]: a["answers"] for a in answers}
    rows = []
    for rec, lab in zip(candidates, labels):
        ans = by_id.get(rec.get("chunk_id", ""), {})
        rows.append((rec, lab.get("label", lab), ans))

    print(f"\n=== characterization v3: {len(rows)} chunks ===")

    def yes(a, n):
        return bool(a.get(n, {}).get("value"))

    pred = [a.get("cardinality", {}).get("value") != "none" for _, _, a in rows]
    gold = [gold_bucket(l) != "other" for _, l, _ in rows]
    print("record detection (cardinality != none):", _binary(pred, gold))

    # attendability vs the event schema
    pred = [yes(a, "attend_on_date") for _, _, a in rows]
    gold = [l.get("target") == "event" for _, l, _ in rows]
    print("attend vs target=event:", _binary(pred, gold))

    pred = [yes(a, "attend_on_date") and yes(a, "has_specific_date") for _, _, a in rows]
    print("attend AND specific_date vs target=event:", _binary(pred, gold))

    gold_single = [gold_bucket(l) == "item_event" for _, l, _ in rows]
    print("attend vs single event bucket:", _binary([yes(a, "attend_on_date") for _, _, a in rows], gold_single))

    cm = collections.Counter(
        (gold_bucket(l), a.get("cardinality", {}).get("value", "none"), yes(a, "attend_on_date"))
        for _, l, a in rows
    )
    print("\ncardinality x attend vs gold bucket:")
    for g in ("list", "item_event", "item_news", "other"):
        for card in ("none", "single", "few", "list"):
            yes_ct = cm.get((g, card, True), 0)
            no_ct = cm.get((g, card, False), 0)
            if yes_ct or no_ct:
                print(f"    {g:11s} cardinality={card:6s} attend=T:{yes_ct:2d} F:{no_ct:2d}")

    # proposed router: attend -> events path, else news path; cardinality -> single/array
    correct = 0
    for _, l, a in rows:
        g = gold_bucket(l)
        card = a.get("cardinality", {}).get("value", "none")
        attend = yes(a, "attend_on_date")
        if card == "none":
            p = "other"
        elif attend:
            p = "list" if card in ("few", "list") else "item_event"
        else:
            p = "list" if card in ("few", "list") else "item_news"
        correct += (p == g)
    print(f"\nproposed router accuracy={correct/len(rows):.2f}")


def _count_bucket(records: int) -> str:
    if records <= 0:
        return "0"
    if records == 1:
        return "1"
    if records == 2:
        return "2"
    return "3+"


def score_v8(candidates: list[dict[str, Any]], answers: list[dict[str, Any]],
             labels: list[dict[str, Any]]) -> None:
    by_id = {a["chunk_id"]: a["answers"] for a in answers}
    v7 = {}
    v7_path = DEFAULT_GOLD / "characterization_v7.jsonl"
    if v7_path.exists():
        v7 = {r["chunk_id"]: r["answers"] for r in load_jsonl(v7_path)}
    rows = []
    for rec, lab in zip(candidates, labels):
        cid = rec.get("chunk_id", "")
        rows.append((cid, lab.get("label", lab), by_id.get(cid, {})))

    print(f"\n=== characterization v8: {len(rows)} chunks ===")

    def count_of(a):
        return a.get("item_count", {}).get("value", "0")

    # exact bucket agreement
    correct = sum(1 for _, l, a in rows if count_of(a) == _count_bucket(int(l.get("records") or 0)))
    print(f"item_count exact-bucket accuracy: {correct/len(rows):.0%}")
    cm = collections.Counter((_count_bucket(int(l.get('records') or 0)), count_of(a)) for _, l, a in rows)
    print("  gold -> pred:")
    for g in ("0", "1", "2", "3+"):
        print(f"    gold {g:4s}", {p: cm.get((g, p), 0) for p in ("0", "1", "2", "3+")})

    # single-item accuracy: the over-labeling failure mode
    gold1 = [b == "1" for b in (_count_bucket(int(l.get('records') or 0)) for _, l, _ in rows)]
    pred1 = [count_of(a) == "1" for _, _, a in rows]
    print("\nsingle-item (pred item_count==1 vs gold records==1):", _binary(pred1, gold1))
    for tag, v7 in (("v7", v7),):
        if v7:
            p7 = [v7.get(cid, {}).get("cardinality", {}).get("value") == "single" for cid, _, _ in rows]
            n_over = sum(1 for g, p in zip(gold1, p7) if g and not p)
            print(f"{tag}: gold single mislabeled few/list = {n_over}/{sum(gold1)}")
    over = sum(1 for g, p in zip(gold1, pred1) if g and not p)
    print(f"v8: gold single mislabeled 2/3+ = {over}/{sum(gold1)}")

    # routing view: skip vs extract, single vs multi
    det_p = [count_of(a) != "0" for _, _, a in rows]
    det_g = [l.get("target") != "none" or int(l.get("records") or 0) > 0 for _, l, _ in rows]
    print("\nrecord detection (item_count != 0):", _binary(det_p, det_g))
    multi_p = [count_of(a) in ("2", "3+") for _, _, a in rows]
    multi_g = [int(l.get("records") or 0) >= 2 for _, l, _ in rows]
    print("multi-item detection (item_count 2/3+):", _binary(multi_p, multi_g))


def score_v9(candidates: list[dict[str, Any]], answers: list[dict[str, Any]],
             labels: list[dict[str, Any]]) -> None:
    by_id = {a["chunk_id"]: a["answers"] for a in answers}
    print(f"\n=== characterization v9 (triage + scoring): {len(answers)} chunks ===")

    # item_count stability (the thing that broke before)
    spread = collections.Counter(a["answers"].get("item_count", {}).get("value") for a in answers)
    print("item_count spread:", dict(spread))

    # accept metrics vs target labels
    tp = fp = fn = tn = 0
    for rec, lab in zip(candidates, labels):
        ans = by_id.get(rec.get("chunk_id", ""), {})
        accepted = (ans.get("is_content", {}).get("value")
                    and ans.get("kind", {}).get("value") not in ("nav", "other")
                    and int(ans.get("relevance", {}).get("value", 0) or 0) >= 2
                    and ans.get("item_count", {}).get("value") != "0")
        gold = (lab.get("label", lab).get("target") != "none")
        tp += accepted and gold; fp += accepted and not gold
        fn += (not accepted) and gold; tn += (not accepted) and not gold
    p = tp / (tp + fp) if tp + fp else 0; r = tp / (tp + fn) if tp + fn else 0
    print(f"accept precision={p:.2f} recall={r:.2f} (tp={tp} fp={fp} fn={fn} tn={tn})")

    # score distribution by gold bucket
    by_gold = collections.defaultdict(list)
    for rec, lab in zip(candidates, labels):
        ans = by_id.get(rec.get("chunk_id", ""), {})
        by_gold[gold_bucket(lab.get("label", lab))].append(scoring.score_answers(ans))
    print("event score by gold bucket (mean / n):")
    for g in ("list", "item_event", "item_news", "other"):
        vals = by_gold.get(g, [])
        if vals:
            print(f"  {g:11s} {sum(vals)/len(vals):5.1f}  n={len(vals)}")

    # score separation: event-ish vs not (inputs)
    item_vals = by_gold.get("item_event", []) + by_gold.get("list", [])
    other_vals = by_gold.get("other", []) + by_gold.get("item_news", [])
    if item_vals and other_vals:
        print(f"  mean score event-y(={sum(item_vals)/len(item_vals):.1f}) "
              f"vs other({sum(other_vals)/len(other_vals):.1f})")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    p.add_argument("--task", choices=sorted(TASKS), default="v1")
    p.add_argument("--decisions", type=Path, default=None)
    p.add_argument("--run", action="store_true", help="call Jev and write the cache")
    p.add_argument("--score", action="store_true", help="score the cached answers")
    p.add_argument("--client", choices=["auto", "openrouter", "typesafe", "stub"], default="auto")
    p.add_argument("--limit", type=int, default=None)
    opts = p.parse_args(argv)
    if opts.decisions is None:
        opts.decisions = DEFAULT_CACHES[opts.task]

    candidates = load_jsonl(opts.gold / "candidates.jsonl")
    labels = load_jsonl(opts.gold / "labels.jsonl")
    if opts.limit:
        candidates, labels = candidates[: opts.limit], labels[: opts.limit]

    if opts.run or not opts.decisions.exists():
        client = jev.make_client(None if opts.client == "auto" else opts.client)
        print(f"client: {type(client).__name__}  model: {getattr(client, 'model', 'stub')}")
        answers = run(candidates, client, TASKS[opts.task])
        opts.decisions.write_text(
            "\n".join(json.dumps(a) for a in answers) + "\n", encoding="utf-8")
        print(f"wrote {len(answers)} decisions -> {opts.decisions}")
    else:
        answers = load_jsonl(opts.decisions)
        print(f"loaded {len(answers)} cached decisions <- {opts.decisions}")

    if opts.score or not opts.run:
        scorer = {"v1": score, "v2": score_v2, "v3": score_v3, "v4": score_v3,
                  "v5": score_v3, "v6": score_v3, "v7": score_v3, "v8": score_v8,
                  "v9": score_v9, "v10": score_v9}[opts.task]
        scorer(candidates, answers, labels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())