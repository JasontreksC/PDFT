"""Client of the Docling service (docling/server.py, its own CPU-only container in docker-compose.yml).

The PDFT container reaches it over the compose network as http://docling:8765; an app running outside Docker uses 127.0.0.1.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Callable

import httpx


class ParseError(RuntimeError):
    pass


_IN_DOCKER = Path("/.dockerenv").exists()
URL = os.environ.get("PDFT_DOCLING_URL") or ("http://docling:8765" if _IN_DOCKER else "http://127.0.0.1:8765")


def _headers() -> dict:
    """Optional bearer token (PDFT_DOCLING_TOKEN, same value as in the service); empty = the service does not check one."""
    t = os.environ.get("PDFT_DOCLING_TOKEN", "").strip()
    return {"Authorization": f"Bearer {t}"} if t else {}


async def health(timeout: float = 3.0) -> dict | None:
    """{"device": "cpu", "docling": "2.x"} when the service is up and the token is accepted, else None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{URL}/health", headers=_headers())
            if r.status_code == 200:
                return r.json()
            return {"ok": False, "error": "토큰이 맞지 않습니다" if r.status_code == 401 else f"HTTP {r.status_code}"}
    except Exception:  # noqa: BLE001
        return None


async def parse_pdf_docling(path: Path, *, table_structure: bool = False,
                            on_status: Callable[[str], None] | None = None) -> dict:
    """Send the PDF to the Docling service, follow its progress, return the parser-result dict (see docling_convert)."""
    say = on_status or (lambda m: None)
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as c:
            say("Docling 서비스로 전송 중")
            with open(path, "rb") as f:
                r = await c.post(f"{URL}/jobs", headers=_headers(), files={"file": (Path(path).name, f, "application/pdf")},
                                 params={"table_structure": str(table_structure).lower()})
            if r.status_code == 401:
                raise ParseError("Docling 서비스 토큰이 맞지 않습니다 (두 컨테이너의 PDFT_DOCLING_TOKEN 이 같아야 합니다).")
            if r.status_code >= 300:
                raise ParseError(f"Docling 서비스 오류 HTTP {r.status_code}: {r.text[:200]}")
            jid = r.json()["id"]
            try:
                while True:
                    await asyncio.sleep(1.0)
                    s = (await c.get(f"{URL}/jobs/{jid}", headers=_headers())).json()
                    if s["status"] == "error":
                        raise ParseError(f"Docling 분석 실패: {s.get('error')}")
                    if s["status"] == "done":
                        break
                    if s.get("total"):
                        say(f"Docling({s.get('device') or '…'}): {s['done']}/{s['total']}쪽")
                    else:
                        say(f"Docling: {s.get('message') or '대기 중'}")
                res = (await c.get(f"{URL}/jobs/{jid}/result", headers=_headers(), timeout=httpx.Timeout(60.0, read=600.0))).json()
            finally:
                try:
                    await c.delete(f"{URL}/jobs/{jid}", headers=_headers())
                except Exception:  # noqa: BLE001
                    pass
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise ParseError("Docling 서비스에 연결할 수 없습니다. `docker compose ps`로 docling 컨테이너가 떠 있는지 확인하세요.") from e
    except httpx.HTTPError as e:
        raise ParseError(f"Docling 서비스 통신 오류: {type(e).__name__}: {e}") from e
    res["seconds"] = round(time.time() - t0, 1)
    return res
