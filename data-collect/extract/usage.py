"""Append-only OpenRouter cost log, one JSON line per paid call.

Every caller shares one API key, so the key's usage total cannot say where the
money goes. Each client records ``caller``, model, tokens and the ``cost``
OpenRouter reports (requested with ``usage: {include: true}``). Summarize with
``python -m extract.usage [--days N]``.

Logging is best-effort: a full disk or unwritable path never fails a call.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "processed" / "llm-usage.jsonl"
_LOCK = threading.Lock()


def log_path() -> Path:
    return Path(os.getenv("LLM_USAGE_LOG") or DEFAULT_PATH)


def default_caller() -> str:
    """``LLM_CALLER`` if set, else the running script's name."""
    return os.getenv("LLM_CALLER") or Path(sys.argv[0] or "python").stem


def record(caller: Optional[str], model: str, usage: Optional[dict[str, Any]], kind: str = "chat") -> None:
    if os.getenv("LLM_USAGE_LOG") == "off":
        return
    usage = usage or {}
    row = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "caller": caller or default_caller(),
        "kind": kind,
        "model": model,
        "promptTokens": usage.get("prompt_tokens"),
        "cachedTokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "completionTokens": usage.get("completion_tokens"),
        "reasoningTokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        "cost": usage.get("cost"),
    }
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row) + "\n"
        with _LOCK, open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def summarize(path: Path, days: float) -> list[tuple[str, str, int, float]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    calls: dict[tuple[str, str], int] = collections.Counter()
    cost: dict[tuple[str, str], float] = collections.defaultdict(float)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if datetime.fromisoformat(row["at"]) < since:
                    continue
            except (ValueError, KeyError):
                continue
            key = (row.get("caller") or "?", row.get("model") or "?")
            calls[key] += 1
            cost[key] += float(row.get("cost") or 0)
    return sorted(((c, m, calls[(c, m)], cost[(c, m)]) for c, m in calls), key=lambda r: -r[3])


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize OpenRouter spend by caller and model.")
    parser.add_argument("--days", type=float, default=1.0)
    parser.add_argument("--log", type=Path, default=None)
    opts = parser.parse_args(argv)
    rows = summarize(opts.log or log_path(), opts.days)
    total = sum(r[3] for r in rows)
    print(f"last {opts.days:g} day(s): ${total:.4f} over {sum(r[2] for r in rows)} calls")
    for caller, model, n, dollars in rows:
        print(f"  ${dollars:8.4f}  {n:5d}  {caller:28s} {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
