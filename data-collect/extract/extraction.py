#!/usr/bin/env python3
"""Stage 7: run triage -> route -> light-model extraction on a chunk.

    python3 -m extract.extraction --gold gold/round2 --n 5
    python3 -m extract.extraction --file processed/ann-arbor-observer --n 5

Jev decides; the router picks a prompt/schema; the light chat model returns
schema-shaped records. Nothing here invents ids/addedAt/localityIndex — those
are assigned later by validation/merge.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import jev, llm, prompts, router


@dataclass
class ExtractionResult:
    chunk_id: str
    route: router.Route
    records: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


def extract_chunk(
    record: dict[str, Any],
    triage: jev.Triage,
    client: llm.ChatClient,
) -> ExtractionResult:
    r = router.route(triage)
    if r.action == "skip":
        return ExtractionResult(record.get("chunk_id", ""), r)
    messages = prompts.build_messages(r.mode, record, r.record_cap)
    try:
        data = client.complete_json(
            messages,
            prompts.schema_for(r.mode),
            schema_name=prompts.schema_name(r.mode),
        )
    except llm.LLMError as exc:
        return ExtractionResult(record.get("chunk_id", ""), r, error=str(exc))
    records = prompts.records_from_response(r.mode, data)[: r.record_cap]
    return ExtractionResult(record.get("chunk_id", ""), r, records=records)


def _load(path: Path, n: int) -> list[dict[str, Any]]:
    if path.is_dir():
        path = path / "candidates.jsonl" if (path / "candidates.jsonl").exists() else path / "chunks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()][:n]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--gold", type=Path)
    src.add_argument("--file", type=Path)
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--model", default=None, help=f"extraction model (default {llm.DEFAULT_MODEL})")
    p.add_argument("--dry-run", action="store_true", help="triage + route only, no extraction call")
    opts = p.parse_args(argv)

    records = _load(opts.gold or opts.file, opts.n)
    jev_client = jev.make_client()
    chat = None if opts.dry_run else llm.make_chat_client(opts.model)

    for record in records:
        triage = jev.triage_record(record, jev_client)
        route = router.route(triage)
        print(f"\n### {record.get('chunk_id')} ({record.get('source_name') or record.get('source_slug')})")
        print(f"  accepted={triage.accepted} item_count={triage.item_count} "
              f"attend={triage.attend_on_date} relevance={triage.relevance} kind={triage.kind}")
        print(f"  route={route.mode}  reason={route.reason}")
        if route.action == "skip" or chat is None:
            continue
        result = extract_chunk(record, triage, chat)
        if result.error:
            print(f"  extraction error: {result.error}")
            continue
        print(f"  extracted {len(result.records)} record(s):")
        for rec in result.records:
            print("   ", json.dumps({k: rec[k] for k in list(rec)[:6]}, ensure_ascii=False)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())