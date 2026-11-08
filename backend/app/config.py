"""Paths, persisted UI settings and the models.json registry."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(os.environ.get("PDFT_DATA_DIR", "/data"))
CONFIG_DIR = Path(os.environ.get("PDFT_CONFIG_DIR", "/config"))
FONT_DIRS = [
    Path(p)
    for p in os.environ.get(
        "PDFT_FONT_DIRS", "/app/fonts:/app/user_fonts:/usr/share/fonts"
    ).split(":")
    if p
]
EXAMPLE_MODELS = Path(os.environ.get("PDFT_EXAMPLE_MODELS", "/app/models.example.json"))
FRONTEND_DIR = Path(os.environ.get("PDFT_FRONTEND_DIR", "/app/frontend"))
MAX_UPLOAD_MB = int(os.environ.get("PDFT_MAX_UPLOAD_MB", "300"))

DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value):
    """Expand ${ENV_VAR} references so keys can live in .env instead of the JSON."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value).strip()
    return value


@dataclass
class ModelEntry:
    id: str
    name: str
    type: str  # openai | anthropic | gemini
    endpoint: str
    api_key: str
    model: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    timeout: float = 300.0
    prompt_cache_key: bool | None = None   # send a stable `prompt_cache_key` so repeated prefixes hit the cache

    @property
    def is_local(self) -> bool:
        return (urlparse(self.endpoint).hostname or "") in LOCAL_HOSTS

    @property
    def use_cache_key(self) -> bool:
        """Explicit models.json value wins; otherwise on for hosts documented to accept the parameter."""
        if self.prompt_cache_key is not None:
            return self.prompt_cache_key
        return (urlparse(self.endpoint).hostname or "") in {"api.openai.com", "api.meta.ai"}

    @property
    def key_ok(self) -> bool:
        return bool(self.api_key) or (self.type == "openai" and self.is_local)

    def public(self) -> dict:
        """What the browser may see. The API key never leaves the server."""
        host = urlparse(self.endpoint).netloc
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "model": self.model,
            "host": host,
            "has_key": self.key_ok,
            "context_window": self.context_window,
        }


class ModelRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, ModelEntry] = {}
        self.errors: list[str] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists() and EXAMPLE_MODELS.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(EXAMPLE_MODELS, self.path)
        entries: dict[str, ModelEntry] = {}
        errors: list[str] = []
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            raw = {"models": []}
            errors.append(f"{self.path} 파일이 없습니다.")
        except json.JSONDecodeError as e:
            raw = {"models": []}
            errors.append(f"models.json 파싱 오류: {e}")
        items = raw.get("models", []) if isinstance(raw, dict) else raw
        for i, m in enumerate(items):
            try:
                typ = str(m.get("type", "openai")).lower()
                if typ not in DEFAULT_ENDPOINTS:
                    raise ValueError(f"type은 openai/anthropic/gemini 중 하나여야 합니다 ({typ})")
                model = str(m["model"]).strip()
                mid = str(m.get("id") or model).strip()
                if mid in entries:
                    raise ValueError(f"id 중복: {mid}")
                cw = m.get("context_window")
                mo = m.get("max_output_tokens")
                entries[mid] = ModelEntry(
                    id=mid,
                    name=str(m.get("name") or mid),
                    type=typ,
                    endpoint=str(_expand(m.get("endpoint") or DEFAULT_ENDPOINTS[typ])).rstrip("/"),
                    api_key=str(_expand(m.get("api_key") or "")),
                    model=model,
                    context_window=int(cw) if cw else None,
                    max_output_tokens=int(mo) if mo else None,
                    params=dict(m.get("params") or {}),
                    headers={k: _expand(v) for k, v in (m.get("headers") or {}).items()},
                    timeout=float(m.get("timeout", 300)),
                    prompt_cache_key=m.get("prompt_cache_key"),
                )
            except Exception as e:  # noqa: BLE001 - report every bad entry, keep the rest
                errors.append(f"models[{i}]: {e}")
        self.entries, self.errors = entries, errors

    def get(self, model_id: str) -> ModelEntry | None:
        return self.entries.get(model_id)

    def public_list(self) -> dict:
        return {"models": [e.public() for e in self.entries.values()], "errors": self.errors}


DEFAULT_SETTINGS = {
    "model_id": None,
    "target_lang": "Korean",
    "default_font": None,
    "auto_compress": True,
    "compress_pct": 70,
    "concurrency": 4,
    "parser_hints": True,          # also use the local Docling layout as hints for text pages (scans always need it)
}


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data = dict(DEFAULT_SETTINGS)
        try:
            self.data.update(json.loads(path.read_text("utf-8")))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def update(self, patch: dict) -> dict:
        with self._lock:
            for k, v in patch.items():
                if k in DEFAULT_SETTINGS:
                    self.data[k] = v
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
            return dict(self.data)
