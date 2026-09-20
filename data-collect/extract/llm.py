#!/usr/bin/env python3
"""Light extraction LLM client (OpenRouter chat completions, JSON schema).

Jev makes the decisions; this is the *generation* tier that turns an accepted
chunk into schema-shaped records. It uses OpenRouter's OpenAI-compatible chat
endpoint with structured outputs, and falls back to plain JSON parsing if the
chosen model rejects `response_format`.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from . import env

# Successor to the benchmarked qwen/qwen-2.5-72b-instruct (retired by
# OpenRouter). Override with OPENROUTER_EXTRACT_MODEL in the environment/.env
# to re-run the model bench and pick a replacement.
DEFAULT_MODEL = "qwen/qwen3-32b"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_RETRYABLE = {429, 500, 502, 503, 524, 529}


class LLMError(RuntimeError):
    pass


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
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        try:
            return self._post(payload)
        except LLMError as exc:
            if schema is not None and "400" in str(exc):
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
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = "unknown"
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.endpoint, data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                choice = (data.get("choices") or [{}])[0]
                content = (choice.get("message") or {}).get("content")
                if not content:
                    raise LLMError(
                        f"empty content (finish_reason={choice.get('finish_reason')})"
                    )
                return json.loads(_strip_fences(content))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                last = f"HTTP {exc.code}: {detail}"
                if exc.code in _RETRYABLE and attempt < self.retries:
                    time.sleep(self.backoff * (2 ** attempt))
                    continue
                raise LLMError(last) from exc
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                    OSError, KeyError, json.JSONDecodeError) as exc:
                last = f"{type(exc).__name__}: {exc}"
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