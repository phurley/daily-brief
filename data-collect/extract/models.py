"""The model registry: repo-root models.json, read once per process.

Roles (``extract``, ``science``, ``review_events`` ...) name the model chain a
job uses; model entries hold what the client needs to know about a model
(Google name, Gemini tier ladder, dated prices). Environment overrides keep
working: a role's ``env`` var replaces the model at that chain position.
"""
from __future__ import annotations

import functools
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Optional

PATH = Path(__file__).resolve().parents[2] / "models.json"


@functools.lru_cache(maxsize=None)
def registry(path: Path = PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def chain(role: str) -> list[dict[str, Any]]:
    """The role's models in fallback order, with env overrides applied.

    An override replaces only the model name at that position; per-model
    settings (reasoning_effort) are kept only if the override names the same
    model. Duplicates are dropped, keeping the first.
    """
    spec = registry()["roles"][role]
    entries = [dict(entry) for entry in spec["chain"]]
    for position, name in enumerate(spec.get("env", [])):
        override = (os.getenv(name) or "").strip()
        if not override:
            continue
        if position < len(entries):
            if entries[position]["model"] != override:
                entries[position] = {"model": override}
        else:
            entries.append({"model": override})
    seen, ordered = set(), []
    for entry in entries:
        if entry["model"] not in seen:
            seen.add(entry["model"])
            ordered.append(entry)
    return ordered


def primary(role: str) -> str:
    return chain(role)[0]["model"]


def max_tokens(role: str) -> Optional[int]:
    return registry()["roles"][role].get("max_tokens")


def model(name: str) -> dict[str, Any]:
    return registry()["models"].get(name, {})


def gemini_name(name: str) -> Optional[str]:
    """Google's id for a model we may call directly, or None (OpenRouter only)."""
    return model(name).get("gemini")


def gemini_tiers(name: str) -> str:
    return model(name).get("tiers", "free,flex,default")


def price(name: str, on: Optional[date] = None) -> Optional[tuple[float, float]]:
    """(input, output) USD per million tokens in effect on ``on`` (default today)."""
    on = (on or date.today()).isoformat()
    current = None
    for entry in model(name).get("prices", []):
        if entry["from"] <= on:
            current = (entry["input"], entry["output"])
    return current
