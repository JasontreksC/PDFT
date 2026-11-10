"""PDFT — FastAPI application (API + static frontend).

Flow: upload PDF -> analyse (the PDF's own text layer gives the text AND where it is printed; the local Docling
service reads scanned pages and gives layout hints) -> frames (pieces of text at their place on the original page)
-> LLM translation per frame -> the translation is typeset into the original pages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

import pymupdf
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .compose.manager import ComposeManager
from .jobs import Job, JobStore, page_sizes
from .llm import clients as llm
from .llm import limits
from .parse import docling_client
from .parse.docling_client import ParseError
from .pdf import PYMU_LOCK, render
from .pdf.fonts import FontManager
from .frames import build as frames_build
from .frames.build import needs_translation
from .translate import pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pdft")

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
registry = config.ModelRegistry(config.CONFIG_DIR / "models.json")
settings = config.SettingsStore(config.DATA_DIR / "settings.json")
fonts = FontManager(config.FONT_DIRS, config.DATA_DIR / "font_cache")
store = JobStore(config.DATA_DIR / "jobs")
composer = ComposeManager(fonts, settings)


def _after_translation_progress(job: Job, final: bool) -> None:
    # while translating, refresh the layout at most every few seconds; at the end, right away
    composer.request(job, min_interval=0.0 if final else 4.0)


runner = pipeline.Runner(settings, _after_translation_progress)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await llm.close()


app = FastAPI(title="PDFT", lifespan=lifespan)


# --------------------------------------------------------------------------- helpers
def _job(jid: str) -> Job:
    job = store.get(jid)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return job


def _model(model_id: str | None) -> config.ModelEntry:
    mid = model_id or settings.data.get("model_id")
    entry = registry.get(mid) if mid else None
    if not entry and registry.entries:
        entry = next(iter(registry.entries.values()))
    if not entry:
        raise HTTPException(400, "사용 가능한 모델이 없습니다. config/models.json을 확인하세요.")
    return entry


def _default_font() -> str | None:
    return fonts.resolve(settings.data.get("default_font"), None)


def _public_settings() -> dict:
    s = dict(settings.data)
    s["default_font"] = _default_font()
    if not registry.get(s.get("model_id") or ""):
        s["model_id"] = next(iter(registry.entries), None)
    return s


def _units(job: Job) -> list[pipeline.Unit]:
    """Units of the pages the user works on (the whole document unless pages are selected)."""
    return pipeline.collect_units(job.doc(), job.selection()) if job.has_doc else []


def _idle_status(job: Job, message: str) -> None:
    """Progress + resting status for the current page selection (not while analysing / translating)."""
    units = _units(job)
    done = sum(1 for u in units if u.ref.get("tgt") is not None)
    job.meta["progress"] = {"done": done, "total": len(units), "message": message, "warnings": 0}
    if job.meta["status"] not in ("ready", "paused", "done"):
        return
    if units and done == len(units):
        job.meta["status"] = "done"
    else:
        anywhere = any(u.ref.get("tgt") is not None and needs_translation(u.src) for u in pipeline.collect_units(job.doc()))
        job.meta["status"] = "paused" if anywhere else "ready"


def _page_states(job: Job) -> list[int]:
    """Per page: 0 not translated, 1 partly, 2 translated, 3 nothing to translate."""
    n = len(job.meta["pages"])
    tot, dn = [0] * n, [0] * n
    for b in job.doc()["blocks"]:
        if needs_translation(b["src"]) and 0 <= b["page"] < n:
            tot[b["page"]] += 1
            dn[b["page"]] += 1 if b.get("tgt") is not None else 0
    return [3 if t == 0 else 2 if d == t else 1 if d else 0 for t, d in zip(tot, dn)]


async def _docling_status() -> dict:
    h = await docling_client.health()
    if h is None:
        return {"available": False, "error": "서비스에 연결할 수 없음 (맥에서 docling_host/run.sh 실행)"}
    if h.get("ok") is False:
        return {"available": False, "error": h.get("error")}
    return {"available": True, "device": h.get("device"), "version": h.get("docling")}


# --------------------------------------------------------------------------- bootstrap / settings
@app.get("/api/bootstrap")
async def bootstrap():
    return {**registry.public_list(), "fonts": fonts.public(), "settings": _public_settings(),
            "docling": await _docling_status()}


class SettingsPatch(BaseModel):
    model_id: str | None = None
    target_lang: str | None = None
    default_font: str | None = None
    auto_compress: bool | None = None          # refresh the background summary automatically while translating
    concurrency: int | None = Field(default=None, ge=1, le=16)
    parser_hints: bool | None = None


@app.put("/api/settings")
async def put_settings(p: SettingsPatch):
    patch = p.model_dump(exclude_none=True)
    if "default_font" in patch and not fonts.has(patch["default_font"]):
        raise HTTPException(400, "알 수 없는 글꼴입니다.")
    if "model_id" in patch and not registry.get(patch["model_id"]):
        raise HTTPException(400, "알 수 없는 모델입니다.")
    changed_font = "default_font" in patch
    settings.update(patch)
    if changed_font:   # the default font changes the look of every block that has no font of its own
        for job in store.jobs.values():
            composer.request(job, delay=0.3)
    return _public_settings()


# --------------------------------------------------------------------------- models
@app.post("/api/models/reload")
async def reload_models():
    registry.load()
    limits._CACHE.clear()  # noqa: SLF001
    return registry.public_list()


@app.get("/api/models/{model_id}/context")
async def model_context(model_id: str, refresh: bool = False):
    entry = registry.get(model_id)
    if not entry:
        raise HTTPException(404, "알 수 없는 모델입니다.")
    limit, source = await limits.lookup(entry, refresh=refresh)
    return {"model_id": model_id, "limit": limit, "source": source, "refresh_at": pipeline.SUMMARY_EVERY}


# --------------------------------------------------------------------------- fonts
@app.get("/api/fonts")
async def list_fonts():
    return {"fonts": fonts.public(), "default": _default_font()}


@app.get("/api/fonts.css")
async def fonts_css():
    return PlainTextResponse(fonts.face_css(), media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get("/api/fonts/file/{fid}")
async def font_file(fid: str):
    ff = fonts.by_id.get(fid)
    if not ff:
        raise HTTPException(404)
    return FileResponse(ff.path, headers={"Cache-Control": "public, max-age=604800"})


# --------------------------------------------------------------------------- jobs
@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": store.list()}


@app.post("/api/jobs")
async def upload(file: UploadFile = File(...)):
    name = (file.filename or "document.pdf").replace("/", "_").replace("\\", "_")
    job = store.create(name)
    job.dir.mkdir(parents=True, exist_ok=True)
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    with open(job.source, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                store.delete(job.id)
                raise HTTPException(413, f"파일이 너무 큽니다 (최대 {config.MAX_UPLOAD_MB}MB).")
            f.write(chunk)
    try:
        with PYMU_LOCK, pymupdf.open(job.source) as d:
            if d.needs_pass:
                raise ValueError("암호가 걸린 PDF는 지원하지 않습니다.")
            if d.page_count == 0:
                raise ValueError("페이지가 없는 PDF입니다.")
        job.meta["pages"] = await asyncio.to_thread(page_sizes, job.source)

        def scans():
            with PYMU_LOCK:
                return frames_build.scan_pages(job.source)
        job.meta["scan_pages"] = len(await asyncio.to_thread(scans))
    except Exception as e:  # noqa: BLE001
        store.delete(job.id)
        raise HTTPException(400, f"PDF를 열 수 없습니다: {e}") from e
    job.save_meta()
    return job.public()


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    return _job(jid).public()


@app.delete("/api/jobs/{jid}")
async def delete_job(jid: str):
    job = _job(jid)
    if job.meta["status"] == "analyzing":
        raise HTTPException(409, "분석 중인 작업은 삭제할 수 없습니다. 분석이 끝난 뒤에 삭제하세요.")
    await runner.cancel(jid)
    composer.forget(jid)
    for p in (job.source, job.composed):
        render.forget(p)
    store.delete(jid)
    return {"ok": True}


# --------------------------------------------------------------------------- analysis
class AnalyzeReq(BaseModel):
    hints: bool = True        # also use the Docling layout as hints for text pages (scanned pages always need Docling)


async def _analyze(job: Job, hints: bool) -> None:
    prev = job.meta["status"]
    had_doc = job.has_doc
    job.meta.update(status="analyzing", error=None)
    job.meta["progress"] = {"done": 0, "total": 0, "message": "문서 분석 중", "warnings": 0}
    job.save_meta()
    try:
        scans = job.meta.get("scan_pages", 0)
        parsed = None
        note = None
        if scans or hints:
            if not scans and not (await _docling_status())["available"]:
                note = "로컬 Docling 서비스가 꺼져 있어 구역 힌트 없이 PDF 글자 위치만으로 분석했습니다."
            else:
                def cb(msg: str) -> None:
                    job.meta["progress"]["message"] = msg

                parsed = await docling_client.parse_pdf_docling(job.source, ocr=bool(scans), on_status=cb)
                job.parse_path.write_text(json.dumps(parsed, ensure_ascii=False), "utf-8")
        if parsed is None:
            job.parse_path.unlink(missing_ok=True)
        job.meta["progress"]["message"] = "문서 구조 정리 중"

        def build():
            with PYMU_LOCK:
                return frames_build.build_document(job.source, parsed)

        doc = await asyncio.to_thread(build)
        if note:
            doc["warnings"].append(note)
        job.reset_context()                         # new structure -> old translations are void
        job.set_doc(doc)
        composer._fit.pop(job.id, None)   # noqa: SLF001
        units = _units(job)
        job.meta["analysis"] = {
            "parser": bool(parsed), "seconds": (parsed or {}).get("seconds"), "blocks": len(doc["blocks"]),
            "stats": doc.get("stats", {}), "warnings": doc.get("warnings", []), "scan_pages": len(doc.get("scan_pages", [])),
        }
        job.meta["progress"] = {"done": sum(1 for u in units if u.ref.get("tgt") is not None), "total": len(units),
                                "message": "분석 완료", "warnings": 0}
        job.meta["status"] = "ready"
        composer.request(job)
    except ParseError as e:
        job.meta.update(status=prev if had_doc else "uploaded", error=str(e))
        job.meta["progress"]["message"] = "분석 실패"
    except Exception as e:  # noqa: BLE001
        log.exception("analysis failed")
        job.meta.update(status=prev if had_doc else "uploaded", error=f"분석 실패: {e}")
        job.meta["progress"]["message"] = "분석 실패"
    finally:
        job.save_meta()


@app.get("/api/docling")
async def docling_status():
    return await _docling_status()


@app.post("/api/jobs/{jid}/analyze")
async def analyze(jid: str, req: AnalyzeReq):
    job = _job(jid)
    if job.meta["status"] in ("analyzing", "translating"):
        raise HTTPException(409, "이미 작업 중입니다.")
    if job.meta.get("scan_pages", 0):
        st = await _docling_status()
        if not st["available"]:
            raise HTTPException(400, f"스캔 쪽을 읽으려면 로컬 Docling 서비스가 필요합니다 (맥에서 docling_host/run.sh 실행): {st['error']}")
    settings.update({"parser_hints": req.hints})
    asyncio.create_task(_analyze(job, req.hints))
    job.meta["status"] = "analyzing"
    job.save_meta()
    return {"ok": True}


@app.post("/api/jobs/{jid}/rebuild")
async def rebuild(jid: str):
    """Re-run the frame builder (on the saved parser result, if there is one: no parser call) and carry
    the existing translations over by exact source text. Used to pick up builder fixes for jobs analysed earlier."""
    job = _job(jid)
    if job.meta["status"] in ("analyzing", "translating") or runner.running(jid):
        raise HTTPException(409, "작업이 진행 중입니다. 끝난 뒤에 다시 시도하세요.")
    parsed = json.loads(job.parse_path.read_text("utf-8")) if job.parse_path.exists() else None
    old = pipeline.collect_units(job.doc()) if job.has_doc else []
    memo = {u.src: u.ref["tgt"] for u in old if u.ref.get("tgt") is not None and u.src}

    def build():
        with PYMU_LOCK:
            return frames_build.build_document(job.source, parsed)

    doc = await asyncio.to_thread(build)
    units = pipeline.collect_units(doc)
    carried = 0
    for u in units:
        if u.ref.get("tgt") is None and u.src in memo:
            u.set(memo[u.src])
            carried += 1
    job.set_doc(doc)
    a = job.meta.get("analysis") or {}
    a.update(blocks=len(doc["blocks"]), stats=doc.get("stats", {}), warnings=doc.get("warnings", []))
    job.meta["analysis"] = a
    job.meta["status"] = "ready" if job.meta["status"] in ("uploaded", "error") else job.meta["status"]
    _idle_status(job, "구조를 다시 정리했습니다")
    job.meta["error"] = None
    job.save_meta()
    done = sum(1 for u in units if u.ref.get("tgt") is not None)
    composer._fit.pop(jid, None)   # noqa: SLF001
    composer.request(job)
    return {"ok": True, "units": len(units), "carried": carried, "remaining": len(units) - done}


@app.get("/api/jobs/{jid}/status")
async def job_status(jid: str, model_id: str | None = None, lang: str | None = None):
    job = _job(jid)
    st = job.meta.get("settings", {})
    entry = _model(model_id or st.get("model_id"))
    gauge = await pipeline.compute_gauge(job, entry, lang or st.get("target_lang") or settings.data["target_lang"])
    return {
        "status": job.meta["status"], "error": job.meta.get("error"), "progress": job.meta.get("progress"),
        "analysis": job.meta.get("analysis"), "compose": job.meta.get("compose"), "gauge": gauge,
        "usage": job.meta.get("usage"), "selected_pages": job.meta.get("selected_pages"),
        "page_state": _page_states(job) if job.has_doc else [],
    }


# --------------------------------------------------------------------------- viewer data
@app.get("/api/jobs/{jid}/pages/{n}/image")
async def page_image(jid: str, n: int, scale: float = Query(2.0, ge=0.25, le=4.0), orig: bool = False):
    job = _job(jid)
    scale = round(scale * 4) / 4            # 0.25 = sidebar thumbnails
    c = job.meta.get("compose")
    if not orig and c and c.get("version", 0) > 0 and job.composed.exists():
        if not 0 <= n < c["pages"]:
            raise HTTPException(404)
        path = job.render_dir / f"c{c['version']}_{n}_{scale}.png"
        pdf = job.composed
    else:
        if not 0 <= n < len(job.meta["pages"]):
            raise HTTPException(404)
        path = job.render_dir / f"o_{n}_{scale}.png"
        pdf = job.source
    if not path.exists():
        data = await asyncio.to_thread(render.render_png, pdf, n, scale)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/jobs/{jid}/pages/{n}/boxes")
async def page_boxes(jid: str, n: int):
    """Clickable regions of composed page n: one per frame (a piece of text)."""
    job = _job(jid)
    if not job.has_doc:
        return {"boxes": []}
    kinds = {b["id"]: b["kind"] for b in job.doc()["blocks"]}
    boxes = []
    for bid, parts in job.positions().items():
        if kinds.get(bid) in (None, "figure"):
            continue
        for p in parts:
            if p["page"] == n:
                boxes.append({"id": bid, "kind": kinds[bid], "rect": p["rect"]})
    return {"boxes": boxes}


def _parser_boxes(parsed: dict | None, n: int) -> list[dict]:
    """The raw items the local Docling service reported for page n (0-based), flattened: what it recognised, as boxes."""
    out: list[dict] = []
    if not parsed:
        return out
    page = next((p for p in (parsed.get("items") or {}).get("pages", []) if p.get("page_number") == n + 1), None)

    def add(it: dict, kind: str | None = None, child: bool = False) -> None:
        bb = (it.get("bbox") or [None])[0]
        if not bb:
            return
        out.append({"type": kind or it.get("type", "text"), "label": bb.get("label"), "child": child,
                    "in_table": bool(it.get("in_table")), "in_cell": bool(it.get("in_cell")),
                    "rect": [bb["x"], bb["y"], bb["x"] + bb["w"], bb["y"] + bb["h"]],
                    "text": (it.get("md") or "")[:200]})

    for it in (page or {}).get("items", []):
        add(it)
        for ch in it.get("items") or []:
            add(ch, "list_item", True)
    return out


@app.get("/api/jobs/{jid}/pages/{n}/layout")
async def page_layout(jid: str, n: int):
    """Analysis view of ORIGINAL page n: the raw boxes of the layout parser (Docling) and the frames (text boxes) that were
    built from the page's text layer. Both in PDF points, top-left origin, like the page image rendered with ?orig=1."""
    job = _job(jid)
    if not 0 <= n < len(job.meta["pages"]):
        raise HTTPException(404)
    parsed = json.loads(job.parse_path.read_text("utf-8")) if job.parse_path.exists() else None
    frames = []
    if job.has_doc:
        for b in job.doc()["blocks"]:
            if b.get("kind") == "text" and b.get("page") == n:
                frames.append({"id": b["id"], "rect": b["bbox"], "lines": b.get("n_lines", 1), "chrome": bool(b.get("chrome")),
                               "text": (b.get("src") or "")[:200]})
    return {"parser": _parser_boxes(parsed, n), "has_parser": parsed is not None, "frames": frames}


@app.get("/api/jobs/{jid}/blocks/{bid}")
async def get_block(jid: str, bid: str):
    job = _job(jid)
    b = job.block(bid) if job.has_doc else None
    if not b:
        raise HTTPException(404)
    return {"id": b["id"], "kind": b["kind"], "style": b["style"], "translated": b.get("tgt") is not None}


# --------------------------------------------------------------------------- translation
class TranslateReq(BaseModel):
    model_id: str
    target_lang: str = "Korean"
    auto_compress: bool = True                            # refresh the background summary while translating
    concurrency: int = Field(default=4, ge=1, le=16)      # pages translated at the same time


@app.post("/api/jobs/{jid}/translate")
async def translate(jid: str, req: TranslateReq):
    job = _job(jid)
    if runner.running(jid):
        raise HTTPException(409, "이미 번역 중입니다.")
    if job.meta["status"] == "analyzing":
        raise HTTPException(409, "문서 분석이 아직 끝나지 않았습니다.")
    if not job.has_doc:
        raise HTTPException(400, "먼저 문서를 분석하세요 (분석 등급을 고르고 '문서 분석').")
    entry = registry.get(req.model_id)
    if not entry:
        raise HTTPException(400, "알 수 없는 모델입니다.")
    if not entry.key_ok:
        raise HTTPException(400, f"'{entry.name}'의 API 키가 설정되지 않았습니다. config/models.json 또는 .env를 확인하세요.")
    sel = job.selection()
    if sel is not None and not sel:
        raise HTTPException(400, "번역할 쪽을 선택하세요 (왼쪽 쪽 목록).")
    units = _units(job)
    if not units:
        raise HTTPException(400, "번역할 텍스트가 없습니다 (스캔본이면 분석 등급을 올려 보세요).")
    if all(u.ref.get("tgt") is not None for u in units):
        raise HTTPException(400, "선택한 쪽은 이미 모두 번역되었습니다. 다른 쪽을 선택하거나 '번역 초기화' 후 다시 시도하세요.")
    settings.update({"model_id": req.model_id, "target_lang": req.target_lang,
                     "auto_compress": req.auto_compress, "concurrency": req.concurrency})
    runner.start(job, entry, req.target_lang, req.auto_compress, req.concurrency)
    return {"ok": True}


@app.post("/api/jobs/{jid}/cancel")
async def cancel(jid: str):
    job = _job(jid)
    await runner.cancel(jid)
    if job.meta["status"] == "translating":
        job.meta["status"] = "paused"
        job.save_meta()
    return {"ok": True}


class ResetReq(BaseModel):
    restore_styles: bool = False      # also put font / weight / size / colour back to what the original page had


@app.post("/api/jobs/{jid}/reset")
async def reset(jid: str, req: ResetReq | None = None):
    """Back to the state before translation for the selected pages (all pages when none are singled out): drops
    their translations and, with `restore_styles`, their style edits. The whole document also loses the internal
    context. The analysed structure stays."""
    job = _job(jid)
    await runner.cancel(jid)
    if job.has_doc:
        sel = job.selection()
        for u in _units(job):
            u.set(None if needs_translation(u.src) else u.src)
        if req and req.restore_styles:
            for b in job.doc()["blocks"]:
                if (sel is None or b["page"] in sel) and b.get("orig_style"):
                    b["style"] = dict(b["orig_style"])
        job.save_doc()
        composer.request(job)
    if job.selection() is None:
        job.reset_context()
    job.meta["status"] = "ready" if job.has_doc else "uploaded"
    job.meta["error"] = None
    if job.has_doc:
        _idle_status(job, "번역 전으로 되돌렸습니다")
    else:
        job.meta["progress"] = {"done": 0, "total": 0, "message": "초기화됨", "warnings": 0}
    job.save_meta()
    return {"ok": True}


class SelectionReq(BaseModel):
    pages: list[int] | None = None       # 0-based page indexes; null = all pages


@app.put("/api/jobs/{jid}/selection")
async def put_selection(jid: str, req: SelectionReq):
    """The pages translate / reset / partial export work on."""
    job = _job(jid)
    if runner.running(jid) or job.meta["status"] in ("translating", "analyzing"):
        raise HTTPException(409, "작업 중에는 쪽 선택을 바꿀 수 없습니다.")
    job.set_selection(req.pages)
    if job.has_doc:
        _idle_status(job, "")
    job.save_meta()
    return {"selected_pages": job.meta.get("selected_pages"), "status": job.meta["status"], "progress": job.meta.get("progress")}


@app.post("/api/jobs/{jid}/compress")
async def compress_now(jid: str, model_id: str | None = None):
    job = _job(jid)
    if runner.running(jid):
        raise HTTPException(409, "번역 중에는 맥락 요약이 자동으로 갱신됩니다.")
    entry = _model(model_id or job.meta.get("settings", {}).get("model_id"))
    lang = job.meta.get("settings", {}).get("target_lang") or settings.data["target_lang"]
    limit, _ = await limits.lookup(entry)
    try:
        did = await pipeline.refresh_summary(job.context(), entry, lang, limit, job)
    except llm.LLMError as e:
        raise HTTPException(502, str(e)) from e
    job.save_context()
    return {"compressed": did}


# --------------------------------------------------------------------------- style editing
class StylePatch(BaseModel):
    font: str | None = Field(default=None)
    weight: int | None = Field(default=None, ge=1, le=1000)
    size: float | None = Field(default=None, ge=2, le=400)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


@app.patch("/api/jobs/{jid}/blocks/{bid}")
async def patch_block(jid: str, bid: str, patch: StylePatch):
    job = _job(jid)
    blk = job.block(bid) if job.has_doc else None
    if not blk or blk["kind"] == "figure":
        raise HTTPException(404)
    fields = patch.model_fields_set
    if "font" in fields:
        if patch.font and not fonts.has(patch.font):
            raise HTTPException(400, "알 수 없는 글꼴입니다.")
        blk["style"]["font"] = patch.font or None  # null = follow the default font
    for k in ("weight", "size", "color"):
        if k in fields and getattr(patch, k) is not None:
            blk["style"][k] = getattr(patch, k)
    job.save_doc()
    composer.request(job, delay=0.3)   # the page is typeset again around the change
    return {"style": blk["style"]}


# --------------------------------------------------------------------------- export
@app.get("/api/jobs/{jid}/export")
async def export_pdf(jid: str, selected: bool = False):
    """The translated PDF; `selected=true` keeps only the selected pages."""
    job = _job(jid)
    if not job.has_doc:
        raise HTTPException(409, "분석된 문서가 없습니다.")
    c = job.meta.get("compose") or {}
    if not job.composed.exists() or c.get("pending") or c.get("busy") or c.get("version", 0) == 0:
        composer.request(job)
        await composer.wait(job)
    c = job.meta.get("compose") or {}
    if c.get("error") or not job.composed.exists():
        raise HTTPException(500, f"PDF 생성 실패: {c.get('error') or '조판 결과가 없습니다'}")
    stem = re.sub(r"\.pdf$", "", job.meta["filename"], flags=re.I)
    sel = job.selection() if selected else None
    if selected and sel is not None:
        if not sel:
            raise HTTPException(400, "선택한 쪽이 없습니다.")

        def cut():
            with PYMU_LOCK:
                d = pymupdf.open(job.composed)
                d.select(sorted(sel))            # the composed PDF has the same pages as the source, in order
                tmp = job.dir / "export_selected.pdf"
                d.save(tmp, garbage=3, deflate=True)
                d.close()
                return tmp

        tmp = await asyncio.to_thread(cut)
        return FileResponse(tmp, media_type="application/pdf", filename=f"{stem}_translated_selected.pdf",
                            background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))
    return FileResponse(job.composed, media_type="application/pdf", filename=f"{stem}_translated.pdf")


@app.get("/api/health")
async def health():
    return {"ok": True}


class _Frontend(StaticFiles):
    """The UI files change with every release: without a Cache-Control header browsers keep serving their old copy for hours
    (heuristic freshness). `no-cache` = always revalidate with the ETag (a 304 costs nothing)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# static frontend (must be last)
if config.FRONTEND_DIR.exists():
    app.mount("/", _Frontend(directory=config.FRONTEND_DIR, html=True), name="frontend")
