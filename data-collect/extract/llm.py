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

from . import env, usage

# Successor to the benchmarked qwen/qwen-2.5-72b-instruct (retired by
# OpenRouter). Override with OPENROUTER_EXTRACT_MODEL in the environment/.env
# to re-run the model bench and pick a replacement.
DEFAULT_MODEL = "qwen/qwen3-32b"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
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

    def get(self, key: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT response FROM responses WHERE key = ?", (key,)).fetchone()
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
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 60.0,
        retries: int = 3,
        backoff: float = 1.0,
        caller: Optional[str] = None,
        cache: bool | ResponseCache = False,
        max_tokens: Optional[int] = None,
        deadline: Optional[float] = None,
    ) -> None:
        env.load_dotenv()
        self.api_key = api_key or env.getenv("OPENROUTER_API_KEY", "OPENROUTE_API_KEY")
        if not self.api_key:
            raise LLMError("missing OPENROUTER_API_KEY")
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        # Names this client's rows in processed/llm-usage.jsonl.
        self.caller = caller
        if cache is True:
            cache = ResponseCache() if os.getenv("LLM_CACHE", "on") != "off" else False
        self.cache: Optional[ResponseCache] = cache or None
        # Caps runaway generations. Gemini 2.5 Flash sometimes loops inside a JSON
        # string until ~65k output tokens (~$0.08/call) and the result never parses.
        self.max_tokens = max_tokens
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
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        key = self.cache.key(payload) if self.cache else None
        if key:
            hit = self.cache.get(key)
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
            "usage": {"include": True},
        }
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
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
            result["usage"] = response.get("usage", {})
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

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Ask OpenRouter to report the billed cost so every call is attributable.
        payload = {**payload, "usage": {"include": True}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = "unknown"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.endpoint, data=body, headers=headers)
            try:
                deadline_at = time.monotonic() + self.deadline
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(_read_body(resp, deadline_at).decode("utf-8"))
                usage.record(self.caller, data.get("model") or self.model, data.get("usage"))
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
                detail = exc.read().decode("utf-8", "replace")[:300]
                last = f"HTTP {exc.code}: {detail}"
                usage.record(self.caller, self.model, None, kind="error")
                if exc.code in _RETRYABLE and attempt < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                raise LLMError(last) from exc
            except DeadlineExceeded as exc:
                # A stalled provider trickles keep-alive bytes, so the socket timeout
                # never fires. Stalls repeat for the same prompt; do not retry.
                usage.record(self.caller, self.model, None, kind="error")
                raise LLMError(f"DeadlineExceeded: {exc}") from exc
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                    OSError, KeyError, json.JSONDecodeError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, (KeyError, json.JSONDecodeError)):
                    usage.record(self.caller, self.model, None, kind="error")  # transport failure, unbilled row
                if attempt < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                raise LLMError(last) from exc
        raise LLMError(last)  # pragma: no cover


def make_chat_client(model: Optional[str] = None, **kwargs: Any) -> ChatClient:
    env.load_dotenv()
    return ChatClient(
        model=model or env.getenv("OPENROUTER_EXTRACT_MODEL") or DEFAULT_MODEL,
        **kwargs,
    )