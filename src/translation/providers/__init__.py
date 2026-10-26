"""Bounded HTTP providers and deterministic offline substitutes."""
from __future__ import annotations

import math
import time
from dataclasses import asdict
from typing import Callable, Protocol

import httpx

from ..config import ModelConfig
from ..schemas import GenerationRequest, GenerationResult
from ..storage import digest


class ProviderError(RuntimeError):
    def __init__(self, kind: str, message: str = "", retryable: bool = False, details: dict | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind, self.retryable = kind, retryable
        self.details = details or {}


class TextGenerator(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult: ...


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> dict: ...


class HTTPProvider:
    def __init__(self, config: ModelConfig, *, client=None, max_requests: int = 100, sleep: Callable = time.sleep, recorder=None):
        self.config = config
        self.client = client or httpx.Client(timeout=httpx.Timeout(config.timeout, connect=20),trust_env=config.trust_env)
        self.max_requests, self.requests_made = max_requests, 0
        self.sleep, self.recorder = sleep, recorder

    def _post(self, route: str, payload: dict) -> dict:
        if not self.config.api_key:
            raise ProviderError("configuration", "missing API credential")
        for attempt in range(self.config.retries + 1):
            if self.requests_made >= self.max_requests:
                raise ProviderError("budget", "request limit reached")
            self.requests_made += 1
            started = time.monotonic()
            event = {"route": route, "attempt": attempt + 1, "request_number": self.requests_made,
                     "payload_hash": digest(payload), "model": self.config.model}
            error = None
            try:
                response = self.client.post(self.config.base_url.rstrip("/") + route,
                                            headers={"Authorization": "Bearer " + self.config.api_key}, json=payload)
                event["http_status"] = response.status_code
                if response.status_code >= 400:
                    kind = "authentication" if response.status_code in {401, 403} else "rate_limit" if response.status_code == 429 else "server" if response.status_code >= 500 else "parameters"
                    # Deliberately do not log raw provider bodies; they can echo credentials/input.
                    raise ProviderError(kind, f"HTTP {response.status_code}", kind in {"rate_limit", "server"})
                try:
                    data = response.json()
                except ValueError:
                    raise ProviderError("invalid_json", "response is not JSON") from None
                if not isinstance(data, dict):
                    raise ProviderError("invalid_response", "expected an object")
                if "error" in data:
                    raise ProviderError("provider_error", "provider reported an error")
                event["request_id"] = data.get("id")
                return data
            except httpx.TimeoutException as exc:
                error = ProviderError("timeout", "remote completion and billing unknown", True,
                                      details={'transport_error':type(exc).__name__})
            except httpx.HTTPError as exc:
                error = ProviderError("network", "HTTP transport failed", True,
                                      details={'transport_error':type(exc).__name__})
            except ProviderError as exc:
                error = exc
            finally:
                event["elapsed_seconds"] = round(time.monotonic() - started, 3)
                event["error"] = error.kind if error else None
                if self.recorder:
                    self.recorder(event)
            if not error.retryable or attempt >= self.config.retries:
                raise error
            self.sleep(min(2 ** attempt, 8))
        raise AssertionError("unreachable")

    def generate(self, request: GenerationRequest) -> GenerationResult:
        payload = {"model": self.config.model, "messages": request.messages, "max_tokens": request.max_tokens,
                   "temperature": request.temperature, "stream": False}
        if request.tools is not None:
            payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice
        if type(self.config.thinking) is bool:
            payload["thinking"] = {"type": "enabled" if self.config.thinking else "disabled"}
        if self.config.reasoning_effort is not None:
            payload["reasoning_effort"] = self.config.reasoning_effort
        started = time.monotonic()
        data = self._post("/chat/completions", payload)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text, reason = message.get("content"), choice.get("finish_reason")
            tool_calls = message.get("tool_calls") or []
        except (KeyError, TypeError, IndexError):
            raise ProviderError("invalid_response", "missing completion choice") from None
        if not isinstance(tool_calls, list):
            raise ProviderError("invalid_response", "tool_calls must be a list")
        if message.get("refusal") or reason in {"content_filter", "sensitive"}:
            raise ProviderError("refusal", "generation was refused/filtered")
        if reason == "length" and not request.allow_truncated:
            raise ProviderError("truncated", "output reached the generation limit", details={
                "model": data.get("model"), "request_id": data.get("id"), "finish_reason": reason,
                "usage": data.get("usage") if isinstance(data.get("usage"), dict) else {},
                "text": text if isinstance(text, str) else "",
            })
        if tool_calls:
            if text is not None and not isinstance(text, str):
                raise ProviderError("invalid_response", "content must be text")
            if data.get("usage") is not None and not isinstance(data["usage"], dict):
                raise ProviderError("invalid_response", "usage must be an object")
            return GenerationResult((text or "").strip(), reason or "tool_calls",
                                    data.get("model"), data.get("id"),
                                    data.get("usage") or {},
                                    round(time.monotonic() - started, 3),
                                    tool_calls=tool_calls)
        if not isinstance(text, str):
            if request.allow_truncated and reason == "length":
                text = ""
            else:
                raise ProviderError("invalid_response", "content must be text")
        if not text.strip() and not (request.allow_truncated and reason == "length"):
            raise ProviderError("empty", "no output text")
        if reason not in {"stop", "length"}:
            raise ProviderError("finish_reason", "completion did not finish normally")
        if data.get("usage") is not None and not isinstance(data["usage"],dict):
            raise ProviderError("invalid_response", "usage must be an object")
        return GenerationResult(text.strip(), reason or "stop", data.get("model"), data.get("id"),
                                data.get("usage") or {}, round(time.monotonic() - started, 3))

    def embed(self, texts: list[str]) -> dict:
        if not texts or any(not t.strip() for t in texts):
            raise ValueError("nonempty embedding inputs required")
        data = self._post("/embeddings", {"model": self.config.model, "input": texts, "dimensions": self.config.dimension})
        rows = data.get("data")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ProviderError("embedding_shape", "embedding item count mismatch")
        try:
            if any(type(r.get("index")) is not int for r in rows) or sorted(r["index"] for r in rows) != list(range(len(texts))):
                raise ValueError()
            vectors = [r["embedding"] for r in sorted(rows, key=lambda r: r["index"])]
            for vector in vectors:
                if not isinstance(vector, list) or len(vector) != self.config.dimension or any(not isinstance(x, (float, int)) or isinstance(x, bool) or not math.isfinite(x) for x in vector) or not any(vector):
                    raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ProviderError("embedding_shape", "invalid indices, dimensions or finite vector values") from None
        return {"vectors": vectors, "model": data.get("model"), "usage": data.get("usage") or {}, "request_id": data.get("id")}


class MockProvider:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests_made = 0

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests_made += 1
        value = self.script.pop(0) if self.script else "离线模拟译文。"
        if isinstance(value, Exception):
            raise value
        if isinstance(value, dict):
            tool_calls = value.get("tool_calls") or []
            finish_reason = value.get("finish_reason") or (
                "tool_calls" if tool_calls else "stop")
            text = value.get("text", "")
            if finish_reason == "length" and not request.allow_truncated:
                raise ProviderError("truncated", "output reached the generation limit",
                                    details={"finish_reason": "length",
                                             "text": text, "usage": {}})
            return GenerationResult(text, finish_reason,
                                    "mock", f"mock-{self.requests_made}", {}, 0,
                                    tool_calls=tool_calls)
        return GenerationResult(value, "stop", "mock", f"mock-{self.requests_made}", {}, 0)

    def embed(self, texts: list[str]) -> dict:
        self.requests_made += 1
        return {"vectors": [[float(int(digest(t)[i:i+2], 16)) / 255 for i in range(0, 16, 2)] for t in texts], "usage": {}, "model": "mock"}


def require_json(text: str, required: dict[str, type]) -> dict:
    """Local validation fallback when native JSON schema is not available."""
    import json
    try:
        value = json.loads(text)
    except ValueError:
        raise ProviderError("structured_output", "invalid JSON") from None
    if not isinstance(value, dict) or any(k not in value or not isinstance(value[k], kind) for k, kind in required.items()):
        raise ProviderError("structured_output", "schema mismatch")
    return value

