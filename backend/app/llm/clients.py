"""Thin async clients for OpenAI-compatible, Anthropic and Gemini chat APIs.

`chat()` is TEXT-only: it accepts nothing but two strings (system, user) and builds the request body exclusively out of
them. The page translation sends the screenshot of a page; that goes through `vision.chat_with_image()`, an explicit
separate function, so an image never leaves by accident.
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

from ..config import ModelEntry


log = logging.getLogger("pdft.llm")


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    truncated: bool = False
    cached_tokens: int | None = None      # part of prompt_tokens served from the provider's prompt cache


_client: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=300, follow_redirects=True)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


async def request_json(
    entry: ModelEntry, method: str, url: str, *, headers: dict | None = None, body: dict | None = None,
    retries: int = 5,
) -> dict:
    last = ""
    for attempt in range(retries + 1):
        delay = min(60.0, 2 ** attempt + random.random())
        try:
            r = await http().request(method, url, headers=headers, json=body, timeout=entry.timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code < 300:
                try:
                    return r.json()
                except ValueError as e:
                    raise LLMError(f"JSON이 아닌 응답: {r.text[:200]}") from e
            last = f"HTTP {r.status_code}: {r.text[:500]}"
            if r.status_code not in _RETRY_STATUS:
                raise LLMError(last)
            ra = r.headers.get("retry-after")
            if ra and ra.replace(".", "", 1).isdigit():
                delay = min(120.0, float(ra))
        if attempt < retries:
            await asyncio.sleep(delay)
    raise LLMError(f"요청 실패 (재시도 초과) — {last}")


def _join(parts) -> str:
    if isinstance(parts, str):
        return parts
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


async def chat(entry: ModelEntry, system: str, user: str, *, max_tokens: int | None = None,
               cache_key: str | None = None) -> LLMResult:
    if not isinstance(system, str) or not isinstance(user, str):
        raise TypeError("LLM requests are text-only: system/user must be str")
    if entry.type == "openai":
        return await _openai(entry, system, user, max_tokens, cache_key)
    if entry.type == "anthropic":
        return await _anthropic(entry, system, user, max_tokens)
    if entry.type == "gemini":
        return await _gemini(entry, system, user, max_tokens)
    raise LLMError(f"지원하지 않는 type: {entry.type}")


async def _openai(entry: ModelEntry, system: str, user: str, max_tokens: int | None, cache_key: str | None = None) -> LLMResult:
    headers = {"Content-Type": "application/json", **entry.headers}
    if entry.api_key:
        headers["Authorization"] = f"Bearer {entry.api_key}"
    params = {k: v for k, v in entry.params.items() if k not in entry.dropped}
    data = None
    for _ in range(len(params) + 1):
        body = {
            "model": entry.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **params,
        }
        if cache_key and entry.use_cache_key and "prompt_cache_key" not in body:
            body["prompt_cache_key"] = cache_key
        try:
            data = await request_json(entry, "POST", f"{entry.endpoint}/chat/completions", headers=headers, body=body)
            break
        except LLMError as e:
            # Models differ in which sampling parameters they take (OpenAI's reasoning models accept only the default
            # `temperature`, some reject `reasoning_effort`...). A 400 that names one of the configured params drops
            # that param for this model from now on and the request is repeated, so a models.json entry written for
            # one model keeps working on its successors.
            msg = str(e)
            bad = [k for k in params if k in msg] if msg.startswith("HTTP 400") else []
            if not bad:
                raise
            for k in bad:
                params.pop(k)
                entry.dropped.add(k)
            log.warning("%s rejected parameter(s) %s - sending without them from now on", entry.id, ", ".join(bad))
    if data is None:
        raise LLMError("요청 실패: 모델이 설정된 params를 모두 거부했습니다.")
    try:
        choice = data["choices"][0]
        text = _join(choice["message"].get("content") or "")
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"예상치 못한 응답 형식: {str(data)[:300]}") from e
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    return LLMResult(text, u.get("prompt_tokens"), u.get("completion_tokens"),
                     choice.get("finish_reason") == "length", cached)


async def _anthropic(entry: ModelEntry, system: str, user: str, max_tokens: int | None) -> LLMResult:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": entry.api_key,
        **entry.headers,
    }
    body = {
        "model": entry.model,
        "max_tokens": int(entry.params.get("max_tokens") or max_tokens or entry.max_output_tokens or 8192),
        "system": system,
        "messages": [{"role": "user", "content": user}],
        **{k: v for k, v in entry.params.items() if k != "max_tokens"},
    }
    data = await request_json(entry, "POST", f"{entry.endpoint}/messages", headers=headers, body=body)
    text = _join(data.get("content") or [])
    u = data.get("usage") or {}
    pt = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (
        u.get("cache_creation_input_tokens") or 0)
    return LLMResult(text, pt or None, u.get("output_tokens"), data.get("stop_reason") == "max_tokens",
                     u.get("cache_read_input_tokens"))


async def _gemini(entry: ModelEntry, system: str, user: str, max_tokens: int | None) -> LLMResult:
    headers = {"Content-Type": "application/json", "x-goog-api-key": entry.api_key, **entry.headers}
    gen = dict(entry.params)
    if entry.max_output_tokens and "maxOutputTokens" not in gen:
        gen["maxOutputTokens"] = entry.max_output_tokens
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": gen,
    }
    data = await request_json(
        entry, "POST", f"{entry.endpoint}/models/{entry.model}:generateContent", headers=headers, body=body)
    try:
        cand = data["candidates"][0]
        text = _join(cand.get("content", {}).get("parts", []))
    except (KeyError, IndexError) as e:
        raise LLMError(f"Gemini가 응답을 거부했거나 비어 있음: {str(data)[:300]}") from e
    u = data.get("usageMetadata") or {}
    return LLMResult(text, u.get("promptTokenCount"), u.get("candidatesTokenCount"),
                     cand.get("finishReason") == "MAX_TOKENS", u.get("cachedContentTokenCount"))
