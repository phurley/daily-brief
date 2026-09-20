#!/usr/bin/env python3
"""Stage 5: cross-source deduplication.

The corpus overlaps heavily: township, library, Patch, and Focus all carry the
same Canton items, and feeds repeat what the deep crawl already fetched. Before
extraction, cluster near-identical records and mark one canonical.

Signals, cheapest first:

1. exact ``content_hash`` (identical normalized text);
2. exact normalized title, for long enough headlines (syndicated stories keep
   their headline even when the body differs);
3. SimHash over token shingles with banded LSH, accepted only when the titles
   are also similar.

Two guards keep this from collapsing chrome:

* only **content-bearing** chunks are eligible (``candidate_hint`` in
  event/news/notice/agenda); undated nav/unknown chunks are left alone;
* groups larger than ``max_group_size`` are considered generic near-duplicates
  and are left unmerged.

Records are anchored by provenance: the canonical pick prefers in-window
content, then the most local ``locality_index``, then the longer text, with a
stable slug/id tie-break. Runs separately for ``chunks`` and ``feed_items``
(feed↔chunk enrichment is handled by the ``article_crawled`` join instead).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_THRESHOLD = 3       # SimHash hamming distance
DEFAULT_MAX_GROUP = 15      # larger groups are treated as generic
_BITS = 64
_BANDS = 4
_BAND_BITS = _BITS // _BANDS
_CONTENT_HINTS = {"event", "news", "notice", "agenda"}

_GENERIC_TITLES = {
    "news", "events", "calendar", "home", "about", "about us", "contact",
    "contact us", "menu", "search", "latest news", "upcoming events",
    "news & events", "announcements", "blog", "staff", "our team", "login",
    "privacy policy", "terms of use", "site map", "sitemap", "hours",
    "related events", "event starts", "nature program schedules",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalize_title(title: str) -> str:
    words = _WORD_RE.findall((title or "").lower())
    return " ".join(words)


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall((title or "").lower()))


def _token_shingles(text: str, size: int = 3, cap: int = 150) -> list[str]:
    words = _WORD_RE.findall((text or "").lower())
    if not words:
        return []
    if len(words) <= size:
        return [" ".join(words)]
    return [" ".join(words[i:i + size]) for i in range(min(len(words) - size + 1, cap))]


def simhash(text: str) -> int:
    """64-bit SimHash over token shingles (stable via blake2b)."""
    vector = [0] * _BITS
    shingles = _token_shingles(text)
    if not shingles:
        return 0
    for shingle in shingles:
        h = int.from_bytes(
            hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(_BITS):
            vector[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(_BITS):
        if vector[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _record_text(record: dict[str, Any]) -> str:
    for key in ("text", "summary"):
        value = record.get(key)
        if value:
            return str(value)
    parts = [record.get("title"), record.get("summary"), record.get("venue")]
    return "\n".join(str(p) for p in parts if p)


def _record_title(record: dict[str, Any]) -> str:
    for key in ("heading", "title", "page_title"):
        value = record.get(key)
        if value:
            return str(value)
    return ""


def _canonical_key(record: dict[str, Any]) -> tuple:
    """Sort key; lower is preferred as canonical."""
    return (
        0 if record.get("in_window", True) else 1,
        record.get("locality_index")
        if isinstance(record.get("locality_index"), int)
        else 9,
        -len(_record_text(record)),
        str(record.get("source_slug", "")),
        str(record.get("chunk_id") or record.get("item_id") or ""),
    )


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _reset(record: dict[str, Any]) -> None:
    record["dup_group"] = None
    record["is_canonical"] = True
    record.pop("duplicate_of", None)


def _title_similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    union = len(a | b)
    return union > 0 and inter / union >= 0.5


def _annotate(
    records: list[dict[str, Any]],
    *,
    threshold: int,
    max_group_size: int,
    eligible: Optional[set[int]] = None,
) -> None:
    """Mutate records in place with dup_group / is_canonical / duplicate_of."""
    n = len(records)
    for record in records:
        _reset(record)
    if n < 2:
        return

    active = [eligible is None or i in eligible for i in range(n)]
    uf = _UnionFind(n)
    sims: list[int] = [0] * n
    titles: list[str] = [""] * n
    tokens: list[frozenset[str]] = [frozenset()] * n

    by_hash: dict[str, int] = {}
    by_title: dict[str, int] = {}
    bands: dict[tuple[int, int], list[int]] = defaultdict(list)

    for i, record in enumerate(records):
        if not active[i]:
            continue
        text = _record_text(record)
        title = normalize_title(_record_title(record))
        titles[i] = title
        tokens[i] = title_tokens(title)

        chash = record.get("content_hash")
        if not chash and text:
            chash = hashlib.sha256(
                re.sub(r"\s+", " ", text).encode("utf-8", "replace")
            ).hexdigest()
        if chash:
            if chash in by_hash:
                uf.union(i, by_hash[chash])
            else:
                by_hash[chash] = i

        if len(title) >= 25 and title not in _GENERIC_TITLES:
            if title in by_title:
                uf.union(i, by_title[title])
            else:
                by_title[title] = i

        if len(text) >= 120 and title not in _GENERIC_TITLES:
            sims[i] = simhash(text)
            for band in range(_BANDS):
                key = (band, (sims[i] >> (band * _BAND_BITS)) & ((1 << _BAND_BITS) - 1))
                bands[key].append(i)

    for bucket in bands.values():
        if len(bucket) < 2 or len(bucket) > 400:
            continue
        for a in range(len(bucket)):
            for b in range(a + 1, len(bucket)):
                i, j = bucket[a], bucket[b]
                if uf.find(i) == uf.find(j):
                    continue
                if hamming(sims[i], sims[j]) > threshold:
                    continue
                if _title_similar(tokens[i], tokens[j]):
                    uf.union(i, j)

    members: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        if active[i]:
            members[uf.find(i)].append(i)

    for indices in members.values():
        if len(indices) < 2 or len(indices) > max_group_size:
            continue  # singleton, or generic near-dupes we leave alone
        canonical = min(indices, key=lambda i: _canonical_key(records[i]))
        canonical_record = records[canonical]
        canonical_id = canonical_record.get("chunk_id") or canonical_record.get("item_id")
        group_id = hashlib.sha1(
            str(canonical_id or canonical).encode()
        ).hexdigest()[:12]
        for i in indices:
            records[i]["dup_group"] = group_id
            records[i]["is_canonical"] = i == canonical
            if i != canonical:
                records[i]["duplicate_of"] = canonical_id
            else:
                records[i].pop("duplicate_of", None)


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def apply(
    out_dir: Path,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    max_group_size: int = DEFAULT_MAX_GROUP,
) -> dict[str, Any]:
    """Dedupe every per-source stream under ``out_dir`` in place."""
    report: dict[str, Any] = {"threshold": threshold, "streams": {}}
    for stream in ("chunks", "feed_items"):
        paths = sorted(out_dir.glob(f"*/{stream}.jsonl"))
        by_source: dict[str, list[dict[str, Any]]] = {}
        flat: list[dict[str, Any]] = []
        for path in paths:
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for record in records:
                record.setdefault("source_slug", path.parent.name)
            by_source[path.parent.name] = records
            flat.extend(records)

        eligible = None
        if stream == "chunks":
            eligible = {
                i for i, r in enumerate(flat)
                if r.get("candidate_hint") in _CONTENT_HINTS
            }
        _annotate(
            flat, threshold=threshold, max_group_size=max_group_size, eligible=eligible
        )

        for path in paths:
            _write_jsonl(path, by_source[path.parent.name])

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in flat:
            if record.get("dup_group"):
                groups[record["dup_group"]].append(record)
        duplicates = sum(len(v) - 1 for v in groups.values())
        sizes = sorted((len(v) for v in groups.values()), reverse=True)
        report["streams"][stream] = {
            "records": len(flat),
            "groups": len(groups),
            "duplicates": duplicates,
            "largest_group": sizes[0] if sizes else 0,
            "eligible": len(eligible) if eligible is not None else len(flat),
        }
    (out_dir / "dedupe.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report