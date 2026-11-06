#!/usr/bin/env python3
"""Light extraction LLM client (OpenRouter chat completions, JSON schema).

Jev makes the decisions; this is the *generation* tier that turns an accepted
chunk into schema-shaped records. It uses OpenRouter's OpenAI-compatible chat
endpoint with structured outputs, and falls back to plain JSON parsing if the
chosen model rejects `response_format`.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import threading
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from . import env, models, usage

# Models, fallbacks, Gemini names/tiers and prices live in models.json (repo root).
DEFAULT_MODEL = models.registry()["roles"]["extract"]["chain"][0]["model"]
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# Google's own OpenAI-compatible endpoint. Models with a "gemini" name in models.json go here
# instead of OpenRouter when a Gemini key is set, down a price ladder:
#   free  FREE_TIER_GEMINI_API_KEY (project without billing; rate-limited, $0)
#   flex  GEMINI_API_KEY, Flex tier (half price, best-effort latency)
#   default  GEMINI_API_KEY, standard tier (full price)
# A rung that is out of quota, busy or slow drops the call to the next one.
# A model's "tiers" in models.json sets its ladder; GEMINI_TIERS (e.g.
# "flex,default") overrides it for every model; LLM_PROVIDER=openrouter switches back.
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_TIER_FALLBACK = {403, 429, 500, 503, 529}  # quota, capacity or a key problem on this rung
_FREE_COOLDOWN = 60.0  # seconds off the free rung after a per-minute 429
_free_blocked_until = 0.0  # shared by every client in the process
_RETRYABLE = {429, 500, 502, 503, 524, 529}
CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "processed", "llm-cache.sqlite")
CACHE_DAYS = 30


class ResponseCache:
    """Exact-request memo for temperature-0 structured calls.

    The key is a hash of the full request (model, messages, schema), so any
    change to the prompt, source text or model is a miss. Callers still run
    their own deterministic validation on every reuse; only the paid model
    answer is reused. SQLite because collect and science can run concurrently.
    Entries unused for ``CACHE_DAYS`` are pruned. ``LLM_CACHE=off`` disables it.
    ``get(max_age=...)`` ignores answers older than that, for callers whose
    retries are meant to ask again (collect reuses answers only across a restart).
    """

    def __init__(self, path: str = CACHE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, model TEXT,"
                       " response TEXT, created REAL, used REAL)")
            db.execute("DELETE FROM responses WHERE used < ?", (time.time() - CACHE_DAYS * 86400,))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def key(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def get(self, key: str, max_age: Optional[float] = None) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT response, created FROM responses WHERE key = ?", (key,)).fetchone()
            if row and max_age is not None and time.time() - row[1] > max_age:
                row = None
            if row:
                db.execute("UPDATE responses SET used = ? WHERE key = ?", (time.time(), key))
        return json.loads(row[0]) if row else None

    def put(self, key: str, model: str, response: dict[str, Any]) -> None:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?)",
                       (key, model, json.dumps(response, ensure_ascii=False), now, now))


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    """Past the hard daily budget and no free-tier route for this call."""


_SPEND_TTL = 300.0  # seconds between re-reads of the usage log
_spend_cache = {"checked": float("-inf"), "dollars": 0.0}


def budget_mode() -> str:
    """"ok", "cheap" (past daily_usd) or "free" (past hard_daily_usd), from the
    last 24h of processed/llm-usage.jsonl, re-read at most every 5 minutes."""
    limits = models.budget()
    if not limits.get("daily_usd") or os.getenv("LLM_USAGE_LOG") == "off":
        return "ok"
    if time.monotonic() - _spend_cache["checked"] > _SPEND_TTL:
        from datetime import datetime, timedelta, timezone
        _spend_cache["dollars"] = usage.spend_since(datetime.now(timezone.utc) - timedelta(hours=24))
        _spend_cache["checked"] = time.monotonic()
    spent = _spend_cache["dollars"]
    if limits.get("hard_daily_usd") and spent > limits["hard_daily_usd"]:
        return "free"
    return "cheap" if spent > limits["daily_usd"] else "ok"


class TruncatedOutput(LLMError):
    """The model stopped at max_tokens; the JSON is incomplete. Never retried."""


_TRUNCATED = {"__truncated__": True}


class DeadlineExceeded(TimeoutError):
    """The response did not finish within the client's wall-clock deadline."""


def _read_body(resp: Any, deadline_at: float) -> bytes:
    """Read a response body, giving up at ``deadline_at`` (time.monotonic)."""
    parts = []
    while True:
        if time.monotonic() > deadline_at:
            raise DeadlineExceeded("response still incomplete at the call deadline")
        chunk = resp.read1(65536)  # returns as soon as any bytes arrive
        if not chunk:
            return b"".join(parts)
        parts.append(chunk)


def _strip_fences(text: str) -> str:
    if not text:
        return ""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


class ChatClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: str = DEFAULT_MODEL,
        endpoint: Optional[str] = None,
        timeout: float = 60.0,
        retries: int = 3,
        backoff: float = 1.0,
        caller: Optional[str] = None,
        cache: bool | ResponseCache = False,
        max_tokens: Optional[int] = None,
        deadline: Optional[float] = None,
        cache_max_age: Optional[float] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        env.load_dotenv()
        self.model = model
        self.tiers: list[str] = []
        self.gemini_keys = {"free": env.getenv("FREE_TIER_GEMINI_API_KEY"), "paid": env.getenv("GEMINI_API_KEY")}
        ladder = [t.strip() for t in os.getenv("GEMINI_TIERS", models.gemini_tiers(model)).split(",") if t.strip()]
        ladder = [t for t in ladder if self.gemini_keys["free" if t == "free" else "paid"]]
        if (api_key is None and endpoint is None and ladder and models.gemini_name(model)
                and os.getenv("LLM_PROVIDER", "auto") != "openrouter"):
            self.provider = "gemini"
            self.tiers = ladder
            self.api_key = ""  # chosen per rung
            self.endpoint = GEMINI_ENDPOINT
        else:
            self.provider = "openrouter"
            self.api_key = api_key or env.getenv("OPENROUTER_API_KEY", "OPENROUTE_API_KEY")
            if not self.api_key:
                raise LLMError("missing OPENROUTER_API_KEY")
            self.endpoint = endpoint or DEFAULT_ENDPOINT
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        # Names this client's rows in processed/llm-usage.jsonl.
        self.caller = caller
        if cache is True:
            cache = ResponseCache() if os.getenv("LLM_CACHE", "on") != "off" else False
        self.cache: Optional[ResponseCache] = cache or None
        self.cache_max_age = cache_max_age
        # Caps runaway generations. Gemini 2.5 Flash sometimes loops inside a JSON
        # string until ~65k output tokens (~$0.08/call) and the result never parses.
        self.max_tokens = max_tokens
        # Thinking budget ("none", "low", "medium", "high"). Thinking tokens count
        # toward max_tokens, so a capped call with default thinking can truncate.
        self.reasoning_effort = reasoning_effort
        # Wall-clock limit per attempt. ``timeout`` only bounds each socket read,
        # and a stalled provider (seen with qwen3-32b on DeepInfra) keeps the
        # connection alive for ~19 minutes before failing.
        self.deadline = deadline if deadline is not None else float(os.getenv("LLM_CALL_DEADLINE", "120"))

    def complete_json(
        self,
        messages: list[dict[str, str]],
        schema: Optional[dict[str, Any]] = None,
        *,
        schema_name: str = "record",
    ) -> dict[str, Any]:
        delegate = self._over_budget_client()
        if delegate is not None:
            return delegate.complete_json(messages, schema, schema_name=schema_name)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        key = self.cache.key(payload) if self.cache else None
        if key:
            hit = self.cache.get(key, self.cache_max_age)
            if hit is not None:
                usage.record(self.caller, self.model, None, kind="cache-hit")
                if hit == _TRUNCATED:
                    raise TruncatedOutput("cached: output hit the token limit for this exact request")
                return hit
        try:
            result = self._complete(payload, schema)
        except TruncatedOutput:
            if key:  # deterministic runaway: remember it so callers can go straight to a fallback
                self.cache.put(key, self.model, _TRUNCATED)
            raise
        if key:
            self.cache.put(key, self.model, result)
        return result

    def _over_budget_client(self) -> Optional["ChatClient"]:
        """None to proceed; else the client to use instead (models.json budget)."""
        mode = budget_mode()
        if mode == "ok" or getattr(self, "_budget_route", None) == mode:
            return None
        cheap = models.budget().get("over_budget_model")
        if mode == "cheap" and self.model == cheap:
            return None
        routes = getattr(self, "_budget_routes", None)
        if routes is None:
            routes = self._budget_routes = {}
        if mode not in routes:
            client = ChatClient(model=cheap, caller=f"{self.caller or usage.default_caller()} (over budget)",
                                cache=self.cache or False, max_tokens=self.max_tokens, deadline=self.deadline,
                                timeout=self.timeout, retries=self.retries, backoff=self.backoff,
                                cache_max_age=self.cache_max_age)
            if mode == "free":
                if "free" not in client.tiers:
                    routes[mode] = None
                else:
                    client.tiers = ["free"]
                    client._budget_route = mode
                    routes[mode] = client
            else:
                client._budget_route = mode
                routes[mode] = client
        if routes[mode] is None:
            raise BudgetExceeded("past the hard daily LLM budget; no free-tier route for this call")
        return routes[mode]

    def _complete(self, payload: dict[str, Any], schema: Optional[dict[str, Any]]) -> dict[str, Any]:
        try:
            return self._post(payload)
        except LLMError as exc:
            if schema is not None and "400" in str(exc) and not isinstance(exc, TruncatedOutput):
                # Model may not support response_format; retry as plain JSON.
                payload.pop("response_format", None)
                return self._post(payload)
            raise

    def complete_once(
        self,
        messages: list[dict[str, str]],
        schema: Optional[dict[str, Any]] = None,
        *,
        schema_name: str = "record",
    ) -> dict[str, Any]:
        """One attempt (for benchmarks); returns raw text, usage, latency, error.

        Requests ``usage.include`` so OpenRouter reports the actual ``cost``.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        tier = self.tiers[0] if self.tiers else None
        body = json.dumps(self._wire(payload, tier), ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._key(tier)}", "Content-Type": "application/json"}
        request = urllib.request.Request(self.endpoint, data=body, headers=headers)
        started = time.time()
        result: dict[str, Any] = {
            "content": None, "data": None, "usage": {}, "latency": None,
            "response_format_used": schema is not None, "error": None,
        }
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                response = json.loads(resp.read().decode("utf-8"))
            content = response["choices"][0]["message"]["content"]
            result["content"] = content
            result["usage"] = self._usage_with_cost(response.get("usage"), tier)
            try:
                result["data"] = json.loads(_strip_fences(content))
            except json.JSONDecodeError as exc:
                result["error"] = f"json: {exc}"
        except urllib.error.HTTPError as exc:
            result["error"] = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                OSError, KeyError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        except (urllib.error.URLError, TimeoutError, OSError, KeyError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["latency"] = round(time.time() - started, 2)
        return result

    def _wire(self, payload: dict[str, Any], tier: Optional[str]) -> dict[str, Any]:
        """The request body for this client's provider."""
        if self.provider == "gemini":
            wire = {**payload, "model": models.gemini_name(self.model)}
            if tier in ("flex", "default"):
                wire["service_tier"] = tier
            return wire
        # Ask OpenRouter to report the billed cost so every call is attributable.
        wire = {**payload, "usage": {"include": True}}
        effort = wire.pop("reasoning_effort", None)
        if effort:
            wire["reasoning"] = {"effort": effort}
        return wire

    def _key(self, tier: Optional[str]) -> str:
        if self.provider != "gemini":
            return self.api_key
        return self.gemini_keys["free" if tier == "free" else "paid"]

    def _first_tier(self) -> Optional[str]:
        usable = [t for t in self.tiers if t != "free" or time.monotonic() >= _free_blocked_until]
        return (usable or self.tiers or [None])[0]

    def _next_tier(self, tier: Optional[str], detail: str = "") -> Optional[str]:
        """The rung below ``tier``, or None at the bottom. Rate limits on the free
        rung also keep later calls off it: for good on a daily quota, else briefly."""
        global _free_blocked_until
        if tier == "free" and ("429" in detail or "RESOURCE_EXHAUSTED" in detail):
            _free_blocked_until = time.monotonic() + (float("inf") if "PerDay" in detail else _FREE_COOLDOWN)
        if tier not in self.tiers:
            return None
        i = self.tiers.index(tier)
        return self.tiers[i + 1] if i + 1 < len(self.tiers) else None

    def _log_model(self, tier: Optional[str]) -> str:
        return f"{self.model} (gemini {tier})" if self.provider == "gemini" else self.model

    def _usage_with_cost(self, raw: Optional[dict[str, Any]], tier: Optional[str]) -> dict[str, Any]:
        """Usage as reported, plus an estimated ``cost`` where the provider gives none."""
        raw = dict(raw or {})
        if self.provider != "gemini" or "cost" in raw:
            return raw
        price = models.price(self.model)
        if tier == "free":
            raw["cost"] = 0.0
        elif price:
            tokens_in = int(raw.get("prompt_tokens") or 0)
            # Thinking tokens are billed as output but left out of completion_tokens.
            tokens_out = max(int(raw.get("total_tokens") or 0) - tokens_in, int(raw.get("completion_tokens") or 0))
            factor = 0.5 if tier in ("flex", "batch") else 1.0
            raw["cost"] = round((tokens_in * price[0] + tokens_out * price[1]) * factor / 1e6, 8)
        return raw

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        tier = self._first_tier()
        last = "unknown"
        attempt = 0  # retries on one rung; dropping to the next Gemini rung is not a retry

        def retry() -> bool:
            nonlocal attempt
            if attempt >= self.retries:
                return False
            time.sleep(self.backoff * (2 ** attempt))
            attempt += 1
            return True

        while True:
            headers = {"Authorization": f"Bearer {self._key(tier)}", "Content-Type": "application/json"}
            body = json.dumps(self._wire(payload, tier), ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(self.endpoint, data=body, headers=headers)
            try:
                deadline_at = time.monotonic() + self.deadline
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(_read_body(resp, deadline_at).decode("utf-8"))
                usage.record(self.caller, self._log_model(tier) if self.provider == "gemini"
                             else data.get("model") or self.model, self._usage_with_cost(data.get("usage"), tier))
                choice = (data.get("choices") or [{}])[0]
                if choice.get("finish_reason") == "length":
                    # Deterministic at temperature 0: retrying re-pays for the same runaway.
                    raise TruncatedOutput(
                        f"output hit the token limit (max_tokens={payload.get('max_tokens')})"
                    )
                content = (choice.get("message") or {}).get("content")
                if not content:
                    raise LLMError(
                        f"empty content (finish_reason={choice.get('finish_reason')})"
                    )
                return json.loads(_strip_fences(content))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                last = f"HTTP {exc.code}: {detail[:300]}"
                usage.record(self.caller, self._log_model(tier), None, kind="error")
                lower = self._next_tier(tier, f"{exc.code} {detail}") if exc.code in _TIER_FALLBACK else None
                if lower:  # out of free quota or Flex capacity: next rung now, rather than wait
                    tier = lower
                    continue
                if exc.code in _RETRYABLE and retry():
                    continue
                raise LLMError(last) from exc
            except DeadlineExceeded as exc:
                # A stalled provider trickles keep-alive bytes, so the socket timeout
                # never fires. Stalls repeat for the same prompt; do not retry,
                # except on the next Gemini rung (a slow free or Flex queue).
                usage.record(self.caller, self._log_model(tier), None, kind="error")
                lower = self._next_tier(tier)
                if lower:
                    tier = lower
                    continue
                raise LLMError(f"DeadlineExceeded: {exc}") from exc
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                    OSError, KeyError, json.JSONDecodeError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, (KeyError, json.JSONDecodeError)):
                    usage.record(self.caller, self._log_model(tier), None, kind="error")  # transport failure, unbilled row
                    lower = self._next_tier(tier) if isinstance(exc, TimeoutError) else None
                    if lower:  # queued past the socket timeout
                        tier = lower
                        continue
                if retry():
                    continue
                raise LLMError(last) from exc


def make_chat_client(model: Optional[str] = None, **kwargs: Any) -> ChatClient:
    env.load_dotenv()
    return ChatClient(
        model=model or models.primary("extract"),
        **kwargs,
    )