"""
Local model client (Ollama on defectserver).

Uses Ollama's native /api/chat rather than its OpenAI-compatible /v1 path:
  - `think` turns qwen3's thinking mode on/off per call, with the reasoning
    returned in a separate field, so the answer stays clean JSON
  - `format` takes a JSON schema and constrains decoding to it (WTBot found
    schema-constrained output stopped small models inventing off-list labels)

config.yaml llm.local.base_url may end in /v1; the native root is derived from it.
Local calls cost nothing, but they're still logged (agent, tokens) for the report.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests
from loguru import logger


class LocalLLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalReply:
    data: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    seconds: float


class OllamaClient:
    def __init__(self, base_url: str, model: str, fallback_model: str | None = None, timeout_sec: float = 120,
                 max_retries: int = 1) -> None:
        root = base_url.rstrip("/")
        self.root = root[:-3] if root.endswith("/v1") else root
        self.model = model
        self.fallback_model = fallback_model
        self.timeout = timeout_sec
        self.max_retries = max_retries

    def chat_json(self, system: str, user: str, schema: dict[str, Any], think: bool,
                  max_tokens: int = 2048) -> LocalReply:
        """One JSON answer. Tries the main model, then the fallback model; transport retries only."""
        last: Exception | None = None
        for model in [m for m in (self.model, self.fallback_model) if m]:
            for attempt in range(self.max_retries + 1):
                t0 = time.monotonic()
                try:
                    r = requests.post(f"{self.root}/api/chat", timeout=self.timeout, json={
                        "model": model, "stream": False, "think": think, "format": schema,
                        "options": {"temperature": 0, "num_predict": max_tokens},
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    })
                except requests.RequestException as exc:          # transport: retry
                    last = exc
                    logger.debug("ollama {} attempt {} failed: {}", model, attempt + 1, exc)
                    continue
                if r.status_code >= 500:
                    last = LocalLLMError(f"HTTP {r.status_code}: {r.text[:120]}")
                    continue
                if r.status_code >= 400:                           # e.g. model missing: try the fallback model
                    last = LocalLLMError(f"HTTP {r.status_code}: {r.text[:120]}")
                    break
                body = r.json()
                content = (body.get("message") or {}).get("content") or ""
                try:
                    data = json.loads(content)
                except json.JSONDecodeError as exc:                # a parsed reply is never re-requested
                    raise LocalLLMError(f"{model} returned non-JSON: {content[:120]!r}") from exc
                return LocalReply(data, model, int(body.get("prompt_eval_count") or 0),
                                  int(body.get("eval_count") or 0), round(time.monotonic() - t0, 2))
        raise LocalLLMError(f"local model unavailable: {last}")

    def healthy(self) -> bool:
        try:
            return requests.get(f"{self.root}/api/tags", timeout=5).status_code == 200
        except requests.RequestException:
            return False
