"""Client of the local Docling service (docling_host/server.py, runs natively on the Mac for Apple-GPU/MPS speed).

The PDFT container reaches it through `host.docker.internal`; an app running natively on the Mac uses 127.0.0.1.
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
URL = os.environ.get("PDFT_DOCLING_URL") or ("http://host.docker.internal:8765" if _IN_DOCKER else "http://127.0.0.1:8765")
TOKEN_FILES = [Path(p) for p in (os.environ.get("PDFT_DOCLING_TOKEN_FILE"), "/docling_host/.token",
                                 str(Path(__file__).resolve().parents[3] / "docling_host" / ".token")) if p]


def token() -> str:
    t = os.environ.get("PDFT_DOCLING_TOKEN")
    if t:
        return t.strip()
    for f in TOKEN_FILES:
        try:
            return f.read_text().strip()
        except OSError:
            continue
    return ""


def _headers() -> dict:
    return {"Authorization": f"Bearer {token()}"}


async def health(timeout: float = 1.5) -> dict | None:
    """{"device": "mps", "docling": "2.x"} when the service is up and the token is accepted, else None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(f"{URL}/health", headers=_headers())
            if r.status_code == 200:
                return r.json()
            return {"ok": False, "error": "토큰이 맞지 않습니다" if r.status_code == 401 else f"HTTP {r.status_code}"}
    except Exception:  # noqa: BLE001
        return None


async def parse_pdf_docling(path: Path, *, table_structure: bool = False, ocr: bool = True, device: str = "auto",
                            on_status: Callable[[str], None] | None = None) -> dict:
    """Send the PDF to the local service, follow its progress, return the parser-result dict (see docling_convert)."""
    say = on_status or (lambda m: None)
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=300.0)) as c:
            say("로컬 Docling 서비스로 전송 중")
            with open(path, "rb") as f:
                r = await c.post(f"{URL}/jobs", headers=_headers(), files={"file": (Path(path).name, f, "application/pdf")},
                                 params={"table_structure": str(table_structure).lower(), "ocr": str(ocr).lower(), "device": device})
            if r.status_code == 401:
                raise ParseError("Docling 서비스 토큰이 맞지 않습니다 (docling_host/.token 을 컨테이너가 읽을 수 있어야 합니다).")
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
        raise ParseError("로컬 Docling 서비스에 연결할 수 없습니다. 맥에서 `docling_host/run.sh`를 실행했는지 확인하세요.") from e
    except httpx.HTTPError as e:
        raise ParseError(f"Docling 서비스 통신 오류: {type(e).__name__}: {e}") from e
    res["seconds"] = round(time.time() - t0, 1)
    return res
