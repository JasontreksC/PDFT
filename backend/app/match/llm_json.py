"""Tolerant reader for the JSON a vision LLM answers with.

The model chooses its own layout ({"extracted_texts": [...]}, {"items": [...]}, fenced in ```json, ...), so instead of
a fixed schema we walk the whole structure and take every object that carries an original-text field.
"""
from __future__ import annotations

import json
import re

ORIG_KEYS = ("original", "original_text", "source", "source_text", "src", "text")
TRANS_KEYS = ("korean_translation", "translation", "translated", "translated_text", "target", "korean", "ko")
LAYOUT_KEYS = ("layout", "type", "kind", "role")
ALIGN_KEYS = ("align", "alignment", "text_align")
LINES_KEYS = ("lines", "original_lines", "text_lines")      # the item's printed text, one string per printed line

_SKIP_KEYS = {"terms", "glossary", "excluded_items", "excluded"}      # lists that are not page text
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_STR = r'"((?:[^"\\]|\\.)*)"'


def _load(text: str):
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except ValueError:
            pass
    return None


def _first(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _lines_of(d: dict) -> list[str] | None:
    """The printed lines of an item: a list of strings under one of LINES_KEYS (a single string with newlines is split)."""
    for k in LINES_KEYS:
        v = d.get(k)
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            got = [x.strip() for x in v if x.strip()]
            if got:
                return got
        elif isinstance(v, str) and "\n" in v:
            got = [x.strip() for x in v.split("\n") if x.strip()]
            if got:
                return got
    return None


def _walk(o, out: list[dict]) -> None:
    if isinstance(o, dict):
        lines = _lines_of(o)
        orig = _first(o, ORIG_KEYS)
        if orig is None and lines:
            orig = " ".join(lines)                       # the model gave the text line by line only
        if orig is not None and not any(isinstance(v, (dict, list)) and _has_items(v) for k, v in o.items()
                                        if k not in _SKIP_KEYS and k not in LINES_KEYS):
            item = {"id": o.get("id", len(out) + 1), "layout": _first(o, LAYOUT_KEYS) or "",
                    "original": orig, "translation": _first(o, TRANS_KEYS) or "", "align": _first(o, ALIGN_KEYS) or ""}
            if lines:
                item["lines"] = lines
            out.append(item)
            return
        for k, v in o.items():
            if k not in _SKIP_KEYS:
                _walk(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, out)


def _has_items(o) -> bool:
    tmp: list[dict] = []
    _walk(o, tmp)
    return bool(tmp)


def _salvage(text: str) -> list[dict]:
    """Truncated / slightly broken JSON: pull the `original` strings (and the translation that follows) out with regexes."""
    items: list[dict] = []
    for m in re.finditer(rf'"(?:{"|".join(ORIG_KEYS)})"\s*:\s*{_STR}', text):
        tail = text[m.end(): m.end() + 4000]
        nxt = re.search(rf'"(?:{"|".join(ORIG_KEYS)})"\s*:', tail)
        tr = re.search(rf'"(?:{"|".join(TRANS_KEYS)})"\s*:\s*{_STR}', tail[: nxt.start()] if nxt else tail)
        try:
            orig = json.loads(f'"{m.group(1)}"')
            trans = json.loads(f'"{tr.group(1)}"') if tr else ""
        except ValueError:
            continue
        items.append({"id": len(items) + 1, "layout": "", "original": orig, "translation": trans, "align": ""})
    if items:
        return items
    # items given line by line: "lines": ["...", "..."]
    arr = rf'"(?:{"|".join(LINES_KEYS)})"\s*:\s*\[((?:\s*{_STR}\s*,?)*)\s*\]'
    for m in re.finditer(arr, text):
        try:
            lines = [x.strip() for x in json.loads(f"[{m.group(1)}]") if x.strip()]
        except ValueError:
            continue
        if not lines:
            continue
        tail = text[m.end(): m.end() + 4000]
        nxt = re.search(rf'"(?:{"|".join(LINES_KEYS)})"\s*:', tail)
        tr = re.search(rf'"(?:{"|".join(TRANS_KEYS)})"\s*:\s*{_STR}', tail[: nxt.start()] if nxt else tail)
        try:
            trans = json.loads(f'"{tr.group(1)}"') if tr else ""
        except ValueError:
            trans = ""
        items.append({"id": len(items) + 1, "layout": "", "original": " ".join(lines), "lines": lines,
                      "translation": trans, "align": ""})
    return items


TERM_SRC = ("src", "source", "term", "original")
TERM_TGT = ("tgt", "target", "translation", "translated")


def _terms(obj) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for key in ("terms", "glossary"):
            v = obj.get(key)
            if isinstance(v, list):
                for t in v:
                    if isinstance(t, dict):
                        a, b = _first(t, TERM_SRC), _first(t, TERM_TGT)
                        if a and b and len(a) <= 120 and len(b) <= 200 and "\n" not in a:
                            out.append((a.strip(), b.strip()))
            elif isinstance(v, dict):
                out.extend((str(a).strip(), str(b).strip()) for a, b in v.items() if a and b and len(str(a)) <= 120)
    return out


def parse_page(text: str) -> tuple[list[dict], list[tuple[str, str]], str | None, bool]:
    """Answer of a page translation -> (items, terms, warning, parsed_ok).
    parsed_ok is True when the answer was valid JSON (an empty `items` list is then a legitimate 'nothing to translate')."""
    obj = _load(text)
    if obj is not None:
        items: list[dict] = []
        _walk(obj, items)
        if items or (isinstance(obj, dict) and any(k in obj for k in ("items", "extracted_texts", "texts", "blocks"))):
            return items, _terms(obj), None, True
    items, warn = parse_items(text)
    return items, [], warn, False


def parse_items(text: str) -> tuple[list[dict], str | None]:
    """-> (items, warning). warning is set when the JSON was broken and the regex fallback had to be used."""
    obj = _load(text)
    items: list[dict] = []
    if obj is not None:
        _walk(obj, items)
        if items:
            return items, None
    items = _salvage(text)
    if items:
        return items, "JSON이 온전하지 않아 원문 항목만 복구했습니다 (응답이 잘렸을 수 있음)"
    return [], "응답에서 원문 항목을 찾지 못했습니다"
