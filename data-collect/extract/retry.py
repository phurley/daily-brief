"""Persistent extraction outcomes and bounded retry scheduling (no network)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_VERSION = 2
TERMINAL = {"extracted", "rejected", "bootstrapped", "exhausted"}


def load_states(path: Path) -> dict[str, dict]:
    """The last entry wins, including a retry after an old extracted entry."""
    states = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                states[row["fp"]] = row
            except (ValueError, KeyError, TypeError):
                continue
    return states


def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def due(state: dict, now: datetime, max_attempts: int) -> bool:
    if state.get("outcome") != "retry" or state.get("attempts", 0) >= max_attempts:
        return False
    next_at = timestamp(state.get("nextRetryAt"))
    return next_at is not None and now >= next_at


def entry(fp: str, outcome: str, previous: dict, now: datetime, *,
          max_attempts: int, backoff_hours: float, record_ids: list[str]) -> dict:
    attempts = (previous.get("attempts", 0) if previous.get("stateVersion") == STATE_VERSION else 0) + 1
    row = dict(fp=fp, ts=now.isoformat(timespec="seconds"), stateVersion=STATE_VERSION,
               attempts=attempts, outcome=outcome, recordIds=record_ids)
    if outcome.startswith("retry:"):
        row["reason"] = outcome.split(":", 1)[1]
        row["outcome"] = "exhausted" if attempts >= max_attempts else "retry"
        if row["outcome"] == "retry":
            delay = min(24.0, backoff_hours * 4 ** min(attempts - 1, 6))
            row["nextRetryAt"] = (now + timedelta(hours=delay)).isoformat(timespec="seconds")
    return row


def legacy_replayable(state: dict, now: datetime, days: float) -> bool:
    """Only explicitly successful old entries in a bounded lookback window."""
    ts = timestamp(state.get("ts"))
    return (state.get("stateVersion", 0) < STATE_VERSION
            and state.get("outcome") == "extracted" and ts is not None
            and now - timedelta(days=days) <= ts <= now)
