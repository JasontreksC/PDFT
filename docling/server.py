"""Docling service for PDFT (runs as its own container, CPU only).

The PDFT container calls it over the compose network (http://docling:8765). It only does layout analysis (no OCR):
PDF in, the parser-result JSON the frame builder reads out ("기본 번역": Docling's regions are the hints that group the
text layer's lines into frames). Nothing leaves your machine. No GPU is used.

Optional auth: set PDFT_DOCLING_TOKEN (same value in both containers) to require `Authorization: Bearer <token>`.
The port is not published to the host, so on the compose network the token is normally not needed.
"""
from __future__ import annotations

import os
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile

import docling_convert as D

TOKEN = os.environ.get("PDFT_DOCLING_TOKEN", "").strip()
THREADS = int(os.environ.get("DOCLING_THREADS") or max(2, os.cpu_count() or 2))
DEVICE = "cpu"                      # CPU only, on purpose

app = FastAPI(title="PDFT Docling service")
_jobs: dict[str, dict] = {}
_conv: dict[bool, object] = {}
_lock = threading.Lock()            # one conversion at a time: the models are not meant to be shared across threads


def auth(authorization: str | None = Header(default=None)) -> None:
    if not TOKEN:
        return
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "invalid or missing token")


@app.get("/health")
def health():
    import docling
    info = {"ok": True, "device": DEVICE, "docling": getattr(docling, "__version__", "?")}
    try:
        import torch
        info["torch"] = torch.__version__
    except Exception:  # noqa: BLE001
        pass
    return info


def _run(jid: str, path: Path, table_structure: bool) -> None:
    job = _jobs[jid]
    try:
        with _lock:
            key = table_structure
            if key not in _conv:
                job["message"] = "모델 불러오는 중"
                _conv[key] = D.make_converter(table_structure=table_structure, device=DEVICE, threads=THREADS)
            job["device"] = DEVICE
            job["message"] = "분석 중"

            def prog(done: int, total: int) -> None:
                job["done"], job["total"] = done, total

            res = D.convert_pdf(path, converter=_conv[key], on_progress=prog)
        job["result"], job["status"] = res, "done"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"
    finally:
        path.unlink(missing_ok=True)


@app.post("/jobs", dependencies=[Depends(auth)])
async def create_job(file: UploadFile = File(...), table_structure: bool = False):
    fd, name = tempfile.mkstemp(suffix=".pdf", prefix="pdft-docling-")
    with os.fdopen(fd, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {"status": "running", "done": 0, "total": 0, "message": "대기 중", "created": time.time(), "result": None, "error": None}
    threading.Thread(target=_run, args=(jid, Path(name), table_structure), daemon=True).start()
    return {"id": jid}


@app.get("/jobs/{jid}", dependencies=[Depends(auth)])
def job_status(jid: str):
    j = _jobs.get(jid)
    if not j:
        raise HTTPException(404)
    return {k: j.get(k) for k in ("status", "done", "total", "message", "error", "device")}


@app.get("/jobs/{jid}/result", dependencies=[Depends(auth)])
def job_result(jid: str):
    j = _jobs.get(jid)
    if not j or j["status"] != "done":
        raise HTTPException(409, "not finished")
    return j["result"]


@app.delete("/jobs/{jid}", dependencies=[Depends(auth)])
def job_delete(jid: str):
    _jobs.pop(jid, None)
    return {"ok": True}
