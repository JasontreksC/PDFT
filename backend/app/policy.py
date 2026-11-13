"""The blanks of the privacy policy page (frontend/privacy_policy.html) that only the operator knows.

The page carries `{{key}}` marks; the values are kept in <data>/policy.json and edited by the administrators in the
dashboard. A blank that is not filled in yet is shown highlighted, with what is expected there."""
from __future__ import annotations

import html
import json
import re

from . import config
from .jobs import _write_json

# key, label in the dashboard, what belongs there (shown highlighted on the page while empty), text kept before the mark
# while empty, a suggestion for the input
FIELDS: list[tuple[str, str, str, str, str]] = [
    ("operator", "운영자 이름 (서문)", "운영자 이름", "", "홍길동 또는 회사 이름"),
    ("dpo_name", "보호책임자 성명 (11조)", "보호책임자 성명", "", "홍길동"),
    ("dpo_email", "보호책임자 전자우편 (11조)", "전자우편 주소", "", "privacy@pdft.org"),
    ("retention_logs", "서버 접속 기록 보유 기간 (3조)", "보관 기간 확정 필요", "서버 프로그램을 갱신하거나 다시 시작할 때 삭제",
     "서버 프로그램을 갱신하거나 다시 시작할 때 삭제 (최대 3개월)"),
    ("meta_contact", "Meta 연락처 (6조)", "연락처 확인 필요", "", "(https://…)"),
    ("meta_country", "Meta 이전 국가 (6조)", "확인 필요", "미국", "미국"),
    ("meta_retention", "Meta 보유·이용 기간 (6조)", "수탁자의 API 이용약관에 따른 기간 확인 필요", "", "번역 요청을 처리한 뒤 최대 30일"),
    ("cf_contact", "Cloudflare 연락처 (6조)", "연락처 확인 필요", "", "(https://…)"),
    ("cf_country", "Cloudflare 이전 국가 (6조)", "확인 필요", "미국 등 Cloudflare가 데이터센터를 운영하는 국가",
     "미국 등 Cloudflare가 데이터센터를 운영하는 국가"),
    ("cf_retention", "Cloudflare 보유 기간 (6조)", "확인 필요", "전송이 끝날 때까지 (저장하지 않고 중계)", "전송이 끝날 때까지 (저장하지 않고 중계)"),
    ("physical", "물리적 조치 (7조)", "서버 설치 장소의 출입 통제 등 실제 조치 기재", "", "서버가 있는 장소에 관계자 외의 출입을 통제합니다."),
    ("effective", "시행일 (14조)", "시행일", "", "2026. 11. 12."),
]
KEYS = {f[0] for f in FIELDS}
DEFAULTS = {"effective": "2026. 11. 12."}
MAX_LEN = 400
_MARK = re.compile(r"\{\{(\w+)\}\}")


def _path():
    return config.DATA_DIR / "policy.json"


def values() -> dict[str, str]:
    try:
        saved = json.loads(_path().read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in KEYS and isinstance(v, str)}}


def save(patch: dict[str, str]) -> dict[str, str]:
    cur = values()
    for k, v in patch.items():
        if k in KEYS:
            cur[k] = " ".join(str(v).split())[:MAX_LEN]
    _write_json(_path(), cur, indent=1)
    return values()


def public() -> dict:
    v = values()
    fields = [{"key": k, "label": label, "value": v.get(k, ""), "suggest": suggest} for k, label, _, _, suggest in FIELDS]
    return {"fields": fields, "missing": sum(not f["value"] for f in fields), "url": "/privacy_policy.html"}


def render(page: str) -> str:
    """Put the values into the page; an empty one stays visible as a highlighted blank."""
    v = values()
    spec = {k: (need, before) for k, _, need, before, _ in FIELDS}

    def put(m: re.Match) -> str:
        key = m.group(1)
        if key not in spec:
            return m.group(0)
        if v.get(key):
            return html.escape(v[key])
        need, before = spec[key]
        return f'{html.escape(before)} <span class="todo">〔{html.escape(need)}〕</span>'.strip()
    return _MARK.sub(put, page)
