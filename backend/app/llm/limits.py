"""Look up a model's context window.

Order: models.json override -> provider API -> built-in table -> conservative default.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from ..config import ModelEntry
from .clients import LLMError, request_json

DEFAULT_LIMIT = 8192
_CACHE: dict[tuple, tuple[float, int, str]] = {}
_TTL = 3600

# Keys different OpenAI-compatible servers use to report the window.
_KEYS = (
    "context_length", "context_window", "max_context_length", "max_model_len",
    "max_input_tokens", "inputTokenLimit", "max_context_window", "n_ctx", "n_ctx_train",
)

# (regex on model name, tokens) — first match wins. Only a fallback.
_BUILTIN = [
    (r"gpt-4\.1", 1_047_576), (r"gpt-5", 400_000), (r"gpt-4o|gpt-4-turbo|chatgpt-4o", 128_000),
    (r"gpt-4-32k", 32_768), (r"gpt-4", 8_192), (r"gpt-3\.5", 16_385), (r"\bo[134]\b|o[134]-", 200_000),
    (r"claude", 200_000),  # the API lookup (max_input_tokens) is authoritative; this is only a fallback
    (r"gemini-1\.5-pro", 2_000_000), (r"gemini", 1_048_576),
    (r"deepseek", 128_000), (r"llama-?3\.[123]|llama-?4", 128_000), (r"qwen", 32_768),
    (r"mistral|mixtral", 32_768), (r"gemma", 8_192),
]


def _find_int(obj, keys=_KEYS) -> int | None:
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        for v in obj.values():
            r = _find_int(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_int(v, keys)
            if r:
                return r
    return None


async def _openai_like(entry: ModelEntry) -> int | None:
    headers = dict(entry.headers)
    if entry.api_key:
        headers["Authorization"] = f"Bearer {entry.api_key}"
    # 1) /models/{id}
    try:
        data = await request_json(entry, "GET", f"{entry.endpoint}/models/{entry.model}", headers=headers, retries=0)
        if (n := _find_int(data)):
            return n
    except LLMError:
        pass
    # 2) /models list (OpenRouter, Together, LM Studio, vLLM ...)
    try:
        data = await request_json(entry, "GET", f"{entry.endpoint}/models", headers=headers, retries=0)
        items = data.get("data") if isinstance(data, dict) else data
        for it in items or []:
            mid = str(it.get("id", "")) if isinstance(it, dict) else ""
            if mid == entry.model or mid.endswith("/" + entry.model):
                if (n := _find_int(it)):
                    return n
    except LLMError:
        pass
    # 3) Ollama native API
    u = urlparse(entry.endpoint)
    if u.port == 11434 or "ollama" in (u.hostname or ""):
        try:
            data = await request_json(entry, "POST", f"{u.scheme}://{u.netloc}/api/show",
                                      body={"model": entry.model}, retries=0)
            for k, v in (data.get("model_info") or {}).items():
                if k.endswith(".context_length") and isinstance(v, int):
                    return v
        except LLMError:
            pass
    return None


async def _anthropic(entry: ModelEntry) -> int | None:
    headers = {"anthropic-version": "2023-06-01", "x-api-key": entry.api_key, **entry.headers}
    try:
        data = await request_json(entry, "GET", f"{entry.endpoint}/models/{entry.model}", headers=headers, retries=0)
        return _find_int(data)
    except LLMError:
        return None


async def _gemini(entry: ModelEntry) -> int | None:
    headers = {"x-goog-api-key": entry.api_key, **entry.headers}
    try:
        data = await request_json(entry, "GET", f"{entry.endpoint}/models/{entry.model}", headers=headers, retries=0)
        return _find_int(data)
    except LLMError:
        return None


def _builtin(model: str) -> int | None:
    name = model.lower()
    for pat, n in _BUILTIN:
        if re.search(pat, name):
            return n
    return None


async def lookup(entry: ModelEntry, *, refresh: bool = False) -> tuple[int, str]:
    """Return (tokens, source). source: config | api | builtin | default."""
    if entry.context_window:
        return entry.context_window, "config"
    key = (entry.type, entry.endpoint, entry.model)
    hit = _CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < _TTL:
        return hit[1], hit[2]
    fetch = {"openai": _openai_like, "anthropic": _anthropic, "gemini": _gemini}[entry.type]
    n = await fetch(entry) if entry.key_ok else None
    if n:
        res = (n, "api")
    elif (b := _builtin(entry.model)):
        res = (b, "builtin")
    else:
        res = (DEFAULT_LIMIT, "default")
    _CACHE[key] = (time.time(), *res)
    return res
