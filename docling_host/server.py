"""Local Docling service for PDFT (runs NATIVELY on the Mac so Docling can use the Apple GPU / MPS).

Docker Desktop on a Mac runs Linux in a VM: no MPS inside the container. This small service runs on the host instead and
the PDFT container calls it (http://host.docker.internal:8765). It only does layout analysis / OCR: PDF in, the parser-result
JSON the frame builder reads out. Nothing leaves your machine.

  ./run.sh            # creates .venv on first use, then starts on 0.0.0.0:8765 (the container reaches it through the host)
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile  # noqa: E402

from app.parse import docling_convert as D  # noqa: E402

TOKEN_FILE = HERE / ".token"
if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(24))
    TOKEN_FILE.chmod(0o600)
TOKEN = os.environ.get("PDFT_DOCLING_TOKEN") or TOKEN_FILE.read_text().strip()

app = FastAPI(title="PDFT Docling service")
_jobs: dict[str, dict] = {}
_conv: dict[tuple, object] = {}
_lock = threading.Lock()            # one conversion at a time: the models are not meant to be shared across threads


def auth(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "invalid or missing token")


def _device() -> str:
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


@app.get("/health")
def health():
    import docling
    info = {"ok": True, "device": _device(), "docling": getattr(docling, "__version__", "?")}
    try:
        import torch
        info["torch"] = torch.__version__
    except Exception:  # noqa: BLE001
        pass
    return info


def _run(jid: str, path: Path, table_structure: bool, ocr: bool, device: str) -> None:
    job = _jobs[jid]
    try:
        with _lock:
            dev = _device() if device == "auto" else device
            key = (table_structure, ocr, dev)
            if key not in _conv:
                job["message"] = "모델 불러오는 중"
                _conv[key] = D.make_converter(table_structure=table_structure, ocr=ocr, device=dev, threads=max(4, (os.cpu_count() or 8)))
            job["device"] = dev
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
async def create_job(file: UploadFile = File(...), table_structure: bool = False, ocr: bool = True, device: str = "auto"):
    if device not in ("auto", "cpu", "mps", "cuda"):
        raise HTTPException(400, "device must be auto|cpu|mps|cuda")
    fd, name = tempfile.mkstemp(suffix=".pdf", prefix="pdft-docling-")
    with os.fdopen(fd, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {"status": "running", "done": 0, "total": 0, "message": "대기 중", "created": time.time(), "result": None, "error": None}
    threading.Thread(target=_run, args=(jid, Path(name), table_structure, ocr, device), daemon=True).start()
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
