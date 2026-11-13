"""Vision calls: one page screenshot + a text instruction -> text.

The page translation sends the screenshot of the page being translated (and nothing else of the document as an image).
`clients.chat()` stays text-only (summary refreshes etc.); images go through this explicit function only.
"""
from __future__ import annotations

import base64
import logging

from ..config import ModelEntry
from .clients import LLMError, LLMResult, _join, request_json

log = logging.getLogger("pdft.vision")


async def chat_with_image(entry: ModelEntry, system: str, user: str, png: bytes, *, max_tokens: int | None = None,
                          cache_key: str | None = None) -> LLMResult:
    if not isinstance(system, str) or not isinstance(user, str) or not isinstance(png, (bytes, bytearray)):
        raise TypeError("chat_with_image(system: str, user: str, png: bytes)")
    b64 = base64.b64encode(png).decode()
    if entry.type == "openai":
        return await _openai(entry, system, user, b64, cache_key)
    if entry.type == "anthropic":
        return await _anthropic(entry, system, user, b64, max_tokens)
    if entry.type == "gemini":
        return await _gemini(entry, system, user, b64)
    raise LLMError(f"지원하지 않는 type: {entry.type}")


async def _openai(entry: ModelEntry, system: str, user: str, b64: str, cache_key: str | None = None) -> LLMResult:
    headers = {"Content-Type": "application/json", **entry.headers}
    if entry.api_key:
        headers["Authorization"] = f"Bearer {entry.api_key}"
    params = {k: v for k, v in entry.params.items() if k not in entry.dropped}
    data = None
    for _ in range(len(params) + 1):
        body = {
            "model": entry.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]},
            ],
            **params,
        }
        if cache_key and entry.use_cache_key and "prompt_cache_key" not in body:
            body["prompt_cache_key"] = cache_key
        try:
            data = await request_json(entry, "POST", f"{entry.endpoint}/chat/completions", headers=headers, body=body)
            break
        except LLMError as e:       # same rule as clients._openai: a 400 naming a param drops that param
            msg = str(e)
            bad = [k for k in params if k in msg] if msg.startswith("HTTP 400") else []
            if not bad:
                raise
            for k in bad:
                params.pop(k)
                entry.dropped.add(k)
            log.warning("%s rejected parameter(s) %s", entry.id, ", ".join(bad))
    if data is None:
        raise LLMError("요청 실패: 모델이 설정된 params를 모두 거부했습니다.")
    try:
        choice = data["choices"][0]
        text = _join(choice["message"].get("content") or "")
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"예상치 못한 응답 형식: {str(data)[:300]}") from e
    u = data.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    return LLMResult(text, u.get("prompt_tokens"), u.get("completion_tokens"), choice.get("finish_reason") == "length", cached)


async def _anthropic(entry: ModelEntry, system: str, user: str, b64: str, max_tokens: int | None) -> LLMResult:
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": entry.api_key, **entry.headers}
    body = {
        "model": entry.model,
        "max_tokens": int(entry.params.get("max_tokens") or max_tokens or entry.max_output_tokens or 8192),
        "system": system,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": user},
        ]}],
        **{k: v for k, v in entry.params.items() if k != "max_tokens"},
    }
    data = await request_json(entry, "POST", f"{entry.endpoint}/messages", headers=headers, body=body)
    u = data.get("usage") or {}
    return LLMResult(_join(data.get("content") or []), u.get("input_tokens"), u.get("output_tokens"),
                     data.get("stop_reason") == "max_tokens", u.get("cache_read_input_tokens"))


async def _gemini(entry: ModelEntry, system: str, user: str, b64: str) -> LLMResult:
    headers = {"Content-Type": "application/json", "x-goog-api-key": entry.api_key, **entry.headers}
    gen = dict(entry.params)
    if entry.max_output_tokens and "maxOutputTokens" not in gen:
        gen["maxOutputTokens"] = entry.max_output_tokens
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}, {"inline_data": {"mime_type": "image/png", "data": b64}}]}],
        "generationConfig": gen,
    }
    data = await request_json(entry, "POST", f"{entry.endpoint}/models/{entry.model}:generateContent", headers=headers, body=body)
    try:
        cand = data["candidates"][0]
        text = _join(cand.get("content", {}).get("parts", []))
    except (KeyError, IndexError) as e:
        raise LLMError(f"Gemini가 응답을 거부했거나 비어 있음: {str(data)[:300]}") from e
    u = data.get("usageMetadata") or {}
    return LLMResult(text, u.get("promptTokenCount"), u.get("candidatesTokenCount"), cand.get("finishReason") == "MAX_TOKENS")
