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
        # Chat APIs say prompt/completion; Jev (OpenRouter and TypeSafe) says input/output.
        "promptTokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "cachedTokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "completionTokens": usage.get("completion_tokens", usage.get("output_tokens")),
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


def _row(raw: bytes) -> Optional[dict[str, Any]]:
    try:
        row = json.loads(raw)
        row["_at"] = datetime.fromisoformat(row["at"])
        return row
    except (ValueError, KeyError, TypeError):
        return None


def recent_rows(path: Path, since: datetime, block: int = 1 << 16) -> list[dict[str, Any]]:
    """Rows logged at or after ``since``, oldest first, reading from the end of
    the file so the cost does not grow with the log's age."""
    if not path.exists():
        return []
    newest_first: list[dict[str, Any]] = []
    with open(path, "rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        carry = b""
        while True:
            step = min(block, position)
            position -= step
            handle.seek(position)
            lines = (handle.read(step) + carry).split(b"\n")
            carry = lines.pop(0) if position > 0 else b""  # first line may be cut
            for raw in reversed(lines):
                row = _row(raw)
                if row is None:
                    continue
                if row["_at"] < since:
                    return [{k: v for k, v in r.items() if k != "_at"} for r in reversed(newest_first)]
                newest_first.append(row)
            if position == 0:
                return [{k: v for k, v in r.items() if k != "_at"} for r in reversed(newest_first)]


def spend_since(since: datetime, path: Optional[Path] = None) -> float:
    """Dollars reported for paid calls since ``since``."""
    return sum(float(r.get("cost") or 0) for r in recent_rows(path or log_path(), since) if r.get("kind") == "chat")


def summarize(path: Path, days: float) -> list[tuple[str, str, int, float, int, int, int]]:
    """Rows of (caller, model, calls, cost, errors, input tokens, output tokens).

    ``errors`` are failed attempts (timeouts, HTTP errors), logged without cost.
    Tokens matter where the provider reports no cost (TypeSafe's direct Jev API).
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    calls: dict[tuple[str, str], int] = collections.Counter()
    errors: dict[tuple[str, str], int] = collections.Counter()
    tokens_in: dict[tuple[str, str], int] = collections.Counter()
    tokens_out: dict[tuple[str, str], int] = collections.Counter()
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
            if row.get("kind") == "error":
                errors[key] += 1
                calls[key] += 0
                continue
            calls[key] += 1
            cost[key] += float(row.get("cost") or 0)
            tokens_in[key] += int(row.get("promptTokens") or 0)
            tokens_out[key] += int(row.get("completionTokens") or 0)
    return sorted(((c, m, calls[(c, m)], cost[(c, m)], errors[(c, m)], tokens_in[(c, m)], tokens_out[(c, m)])
                   for c, m in calls), key=lambda r: (-r[3], -r[2]))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize OpenRouter spend by caller and model.")
    parser.add_argument("--days", type=float, default=1.0)
    parser.add_argument("--log", type=Path, default=None)
    opts = parser.parse_args(argv)
    rows = summarize(opts.log or log_path(), opts.days)
    total = sum(r[3] for r in rows)
    print(f"last {opts.days:g} day(s): ${total:.4f} over {sum(r[2] for r in rows)} calls, "
          f"{sum(r[4] for r in rows)} failed attempts")
    print(f"  {'cost':>9s}  {'calls':>5s}  {'fail':>4s}  {'in tok':>9s}  {'out tok':>8s}  caller / model")
    for caller, model, n, dollars, failed, tin, tout in rows:
        print(f"  ${dollars:8.4f}  {n:5d}  {failed:4d}  {tin:9d}  {tout:8d}  {caller:28s} {model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
