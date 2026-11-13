"""PDFT — FastAPI application (API + static frontend).

Flow: upload PDF -> per page, in one of two modes (setting `translate_mode`):
  basic (기본 번역):    Docling's layout regions are the hints that group the PDF's text layer into frames; their text goes
                        to a text model
  advanced (고급 번역): the page SCREENSHOT goes to a vision model, which returns the text worth translating as
                        {original, translation} items; every original is matched against the text layer (that gives the frame)
-> the translations are typeset into the original pages (font size / layout adapt to their length).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from contextlib import asynccontextmanager

import pymupdf
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Literal

from . import auth, config, policy
from .auth import routes as accounts
from .auth import store as members
from .compose.manager import ComposeManager
from .jobs import Job, JobStore, page_sizes
from .llm import clients as llm
from .llm import limits
from .pdf import PYMU_LOCK, render
from .pdf.fonts import FontManager
from .frames import basic as frames_basic
from .frames import build as frames_build
from .parse import docling_client
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
    if final:                            # the activity log: how the run ended
        p = job.meta.get("progress") or {}
        _note(job.meta.get("owner"), "finish", job, status=job.meta.get("status"), done=p.get("done"), total=p.get("total"),
              usage=job.meta.get("usage"))


_notes: set[asyncio.Task] = set()


def _note(user_id: str | None, kind: str, job: Job | None = None, **detail) -> None:
    """Record what a member did, without making the request wait for the database."""
    if not auth.ENABLED or not user_id:
        return
    t = asyncio.get_running_loop().create_task(members.record(user_id, kind, job, **detail))
    _notes.add(t)
    t.add_done_callback(_notes.discard)


runner = pipeline.Runner(settings, _after_translation_progress)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if auth.ENABLED:                     # fail at start, not at the first login, when the login cannot work
        from . import db
        accounts.client()
        await db.connect()
        log.info("login: Google accounts (public URL: %s)", auth.PUBLIC_URL or "from the request")
    else:
        log.warning("login is OFF (PDFT_AUTH=off): everyone who reaches this server is the same local user")
    yield
    await llm.close()
    if auth.ENABLED:
        await db.close()


# every /api call belongs to a signed-in member (see auth/routes.py); the static frontend below needs no login
app = FastAPI(title="PDFT", lifespan=lifespan, dependencies=[Depends(accounts.require_user)])
app.include_router(accounts.router)


# --------------------------------------------------------------------------- helpers
def _mine(job: Job) -> bool:
    """A document belongs to the member who uploaded it. Documents from before there were members (no owner) are the
    administrators'. Without login (PDFT_AUTH=off) everything is the local user's."""
    if not auth.ENABLED:
        return True
    u = auth.current()
    owner = job.meta.get("owner")
    return u is not None and (owner == u.id or (owner is None and u.is_admin))


def _job(jid: str, view: bool = False) -> Job:
    """The member's own document. `view`: an administrator may also LOOK at anyone's (the dashboard review); never change it."""
    job = store.get(jid)
    u = auth.current()
    if not job or not (_mine(job) or (view and u is not None and u.is_admin)):
        raise HTTPException(404, "작업을 찾을 수 없습니다.")       # someone else's document does not exist for you
    return job


async def _drop_job(job: Job) -> None:
    await runner.cancel(job.id)
    composer.forget(job.id)
    for p in (job.source, job.composed):
        render.forget(p)
    store.delete(job.id)


async def _purge_documents(user_id: str) -> int:
    """A member leaves or is removed: their documents go with them."""
    mine = [j for j in list(store.jobs.values()) if j.meta.get("owner") == user_id]
    for job in mine:
        await _drop_job(job)
    return len(mine)


def _count_documents() -> dict[str, int]:
    out: dict[str, int] = {}
    for j in store.jobs.values():
        if j.meta.get("owner"):
            out[j.meta["owner"]] = out.get(j.meta["owner"], 0) + 1
    return out


accounts.purge_documents = _purge_documents
accounts.count_documents = _count_documents


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


def _mode() -> str:
    m = settings.data.get("translate_mode")
    return m if m in pipeline.MODES else "basic"


async def _docling_status() -> dict:
    h = await docling_client.health()
    if h is None:
        return {"available": False, "error": "서비스에 연결할 수 없음 (docling 컨테이너 확인)"}
    if h.get("ok") is False:
        return {"available": False, "error": h.get("error") or "응답 오류"}
    return {"available": True, "device": h.get("device"), "version": h.get("docling")}


def _build_skeleton(job: Job) -> None:
    """The empty document (no LLM): which pages can be translated at all. Cheap; runs at upload."""
    with PYMU_LOCK:
        doc = frames_build.build_document(job.source)
    job.reset_context()
    job.set_doc(doc)
    job.meta["analysis"] = {"blocks": 0, "stats": doc.get("stats", {}), "warnings": doc.get("warnings", []),
                            "scan_pages": len(doc.get("scan_pages", [])), "translatable": doc["stats"]["translatable"]}
    job.meta["status"] = "ready"
    job.meta["error"] = None
    _idle_status(job, "준비됨")           # (the composer is asked by the async callers: this runs in a thread)


def _idle_status(job: Job, message: str) -> None:
    """Progress + resting status for the current page selection (not while translating)."""
    doc = job.doc()
    sel = job.selection()
    can = frames_build.translatable_pages(doc)
    scope = can if sel is None else can & sel
    todo = pipeline.pages_to_do(doc, sel)
    job.meta["progress"] = {"done": len(scope) - len(todo), "total": len(scope), "message": message, "warnings": 0}
    if job.meta["status"] not in ("ready", "paused", "done"):
        return
    if scope and not todo:
        job.meta["status"] = "done"
    else:
        job.meta["status"] = "paused" if len(pipeline.pages_to_do(doc, None)) < len(can) else "ready"


# --------------------------------------------------------------------------- bootstrap / settings
@app.get("/api/bootstrap")
async def bootstrap():
    return {**registry.public_list(), "fonts": fonts.public(), "settings": _public_settings(), "docling": await _docling_status()}


@app.get("/api/docling")
async def docling_status():
    return await _docling_status()


class SettingsPatch(BaseModel):
    model_id: str | None = None
    target_lang: str | None = None
    default_font: str | None = None
    auto_compress: bool | None = None          # refresh the background summary automatically while translating
    concurrency: int | None = Field(default=None, ge=1, le=16)
    image_long_side: int | None = Field(default=None, ge=800, le=4000)   # screenshot size sent to the model
    layout_mode: Literal["place", "flow"] | None = None   # keep every translation in its place / stack the free frames
    translate_mode: Literal["basic", "advanced"] | None = None   # Docling hints + text model / page screenshots + vision model


@app.put("/api/settings")
async def put_settings(p: SettingsPatch):
    accounts.admin()                     # server-wide: the model, the speed, the layout of every document
    patch = p.model_dump(exclude_none=True)
    if "default_font" in patch and not fonts.has(patch["default_font"]):
        raise HTTPException(400, "알 수 없는 글꼴입니다.")
    if "model_id" in patch and not registry.get(patch["model_id"]):
        raise HTTPException(400, "알 수 없는 모델입니다.")
    relayout = "default_font" in patch or ("layout_mode" in patch and patch["layout_mode"] != settings.data.get("layout_mode"))
    settings.update(patch)
    if relayout:   # the default font / the layout mode change the look of every page
        for job in store.jobs.values():
            composer.request(job, delay=0.3)
    return _public_settings()


# --------------------------------------------------------------------------- models
@app.post("/api/models/reload")
async def reload_models():
    accounts.admin()
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
    return {"jobs": [j for j in store.list() if _mine(store.jobs[j["id"]])]}


@app.post("/api/jobs")
async def upload(file: UploadFile = File(...)):
    name = (file.filename or "document.pdf").replace("/", "_").replace("\\", "_")
    job = store.create(name, owner=accounts.user().id if auth.ENABLED else None)
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
        await asyncio.to_thread(_build_skeleton, job)
        composer.request(job)
        job.meta["scan_pages"] = len(job.doc().get("scan_pages", []))
    except Exception as e:  # noqa: BLE001
        store.delete(job.id)
        raise HTTPException(400, f"PDF를 열 수 없습니다: {e}") from e
    job.save_meta()
    _note(job.meta.get("owner"), "upload", job, pages=len(job.meta["pages"]))
    return job.public()


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    job = _job(jid)
    if not job.has_doc and job.meta["status"] in ("uploaded", "analyzing"):     # made by an older version: no skeleton yet
        await asyncio.to_thread(_build_skeleton, job)
        composer.request(job)
        job.save_meta()
    return job.public()


@app.delete("/api/jobs/{jid}")
async def delete_job(jid: str):
    job = _job(jid)
    _note(job.meta.get("owner"), "delete", job, pages=len(job.meta["pages"]))
    await _drop_job(job)
    return {"ok": True}


@app.get("/api/jobs/{jid}/status")
async def job_status(jid: str, model_id: str | None = None, lang: str | None = None):
    job = _job(jid)
    st = job.meta.get("settings", {})
    entry = _model(model_id or st.get("model_id"))
    mode = st.get("mode") if runner.running(jid) and st.get("mode") in pipeline.MODES else _mode()
    gauge = await pipeline.compute_gauge(job, entry, lang or st.get("target_lang") or settings.data["target_lang"], mode)
    return {
        "status": job.meta["status"], "error": job.meta.get("error"), "progress": job.meta.get("progress"),
        "analysis": job.meta.get("analysis"), "compose": job.meta.get("compose"), "gauge": gauge,
        "usage": job.meta.get("usage"), "selected_pages": job.meta.get("selected_pages"),
        "page_state": pipeline.page_states(job.doc()) if job.has_doc else [], "mode": mode,
    }


# --------------------------------------------------------------------------- viewer data
@app.get("/api/jobs/{jid}/pages/{n}/image")
async def page_image(jid: str, n: int, scale: float = Query(2.0, ge=0.25, le=4.0), orig: bool = False):
    job = _job(jid, view=True)
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


@app.get("/api/jobs/{jid}/pages/{n}/layout")
async def page_layout(jid: str, n: int):
    """Analysis view of ORIGINAL page n, in PDF points (top-left origin, like the page image rendered with ?orig=1):
    the frames (the translation units), the text that stays as printed (`excluded`), the items of the model that could not be
    found in the text layer or that the model did not translate (`lost`), and - for a page translated in "basic" mode -
    the regions Docling found (`parser`)."""
    job = _job(jid)
    if not 0 <= n < len(job.meta["pages"]):
        raise HTTPException(404)
    frames, excluded, lost, parser, mode = [], [], [], [], None
    if job.has_doc:
        doc = job.doc()
        for b in doc["blocks"]:
            if b.get("kind") == "text" and b.get("page") == n:
                frames.append({"id": b["id"], "rect": b["bbox"], "lines": b.get("n_lines", 1), "chrome": bool(b.get("chrome")),
                               "text": (b.get("src") or "")[:200]})
        excluded = doc.get("untouched", {}).get(str(n), [])
        pg = doc.get("pages", {}).get(str(n)) or {}
        lost = pg.get("lost", [])
        mode = pg.get("mode") or ("advanced" if pg else None)
        if mode == "basic" and pg.get("hints"):
            parser = frames_basic.parser_boxes(pipeline.load_parse(job).get(n))
    return {"frames": frames, "excluded": excluded, "lost": lost, "parser": parser, "mode": mode}


@app.get("/api/jobs/{jid}/blocks/{bid}")
async def get_block(jid: str, bid: str):
    job = _job(jid)
    b = job.block(bid) if job.has_doc else None
    if not b:
        raise HTTPException(404)
    return {"id": b["id"], "kind": b["kind"], "style": b["style"], "translated": b.get("tgt") is not None}


# --------------------------------------------------------------------------- translation
class TranslateReq(BaseModel):
    model_id: str | None = None                           # administrators only; members use the server's model and speed
    target_lang: str = Field(default="Korean", min_length=2, max_length=60)
    auto_compress: bool = True                            # refresh the background summary while translating
    concurrency: int = Field(default=4, ge=1, le=16)      # pages translated at the same time
    mode: Literal["basic", "advanced"] | None = None      # default: the setting `translate_mode`


@app.post("/api/jobs/{jid}/translate")
async def translate(jid: str, req: TranslateReq):
    job = _job(jid)
    if runner.running(jid):
        raise HTTPException(409, "이미 번역 중입니다.")
    if not job.has_doc:
        raise HTTPException(400, "문서를 준비하지 못했습니다. PDF를 다시 열어 보세요.")
    boss = accounts.user().is_admin
    entry = registry.get(req.model_id) if boss and req.model_id else _model(None)
    if not entry:
        raise HTTPException(400, "알 수 없는 모델입니다.")
    if not entry.key_ok:
        raise HTTPException(400, f"'{entry.name}'의 API 키가 설정되지 않았습니다. config/models.json 또는 .env를 확인하세요."
                            if boss else "번역 서버 설정이 아직 끝나지 않았습니다. 관리자에게 문의해 주세요.")
    auto = req.auto_compress if boss else bool(settings.data.get("auto_compress", True))
    conc = req.concurrency if boss else int(settings.data.get("concurrency") or 4)
    mode = req.mode or _mode()
    doc = job.doc()
    sel = job.selection()
    if sel is not None and not sel:
        raise HTTPException(400, "번역할 쪽을 선택하세요 (왼쪽 쪽 목록).")
    can = frames_build.translatable_pages(doc)
    if not (can if sel is None else can & sel):
        raise HTTPException(400, "번역할 쪽이 없습니다 (표지·스캔·회전 쪽은 원본 그대로 둡니다).")
    if not pipeline.pages_to_do(doc, sel):
        raise HTTPException(400, "선택한 쪽은 이미 모두 번역되었습니다. 다른 쪽을 선택하거나 '번역 지우기' 후 다시 시도하세요.")
    # the language and the mode are the member's own choice for this run: nothing is written to the server settings
    _note(job.meta.get("owner"), "translate", job, mode=mode, lang=req.target_lang, model=entry.id,
          pages=len(pipeline.pages_to_do(doc, sel)))
    runner.start(job, entry, req.target_lang, auto, conc, mode)
    return {"ok": True}


@app.post("/api/jobs/{jid}/cancel")
async def cancel(jid: str):
    job = _job(jid)
    await runner.cancel(jid)
    if job.meta["status"] == "translating":
        job.meta["status"] = "paused"
        job.save_meta()
    return {"ok": True}


@app.post("/api/jobs/{jid}/reset")
async def reset(jid: str):
    """Back to the state before translation for the selected pages (all pages when none are singled out): their frames
    (translations and style edits) are dropped. The whole document also loses the internal context."""
    job = _job(jid)
    await runner.cancel(jid)
    if job.has_doc:
        doc = job.doc()
        sel = job.selection()
        pages = set(range(doc["page_count"])) if sel is None else sel
        _note(job.meta.get("owner"), "reset", job, pages=len(pages))
        doc["blocks"] = [b for b in doc["blocks"] if b["page"] not in pages]
        for i in pages:
            doc.get("untouched", {}).pop(str(i), None)
            doc.get("pages", {}).pop(str(i), None)
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
    if runner.running(jid) or job.meta["status"] == "translating":
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
    entry = _model((model_id if accounts.user().is_admin else None) or job.meta.get("settings", {}).get("model_id"))
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
    job = _job(jid, view=not selected)
    if _mine(job):
        _note(job.meta.get("owner"), "export", job, selected=selected)
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


# --------------------------------------------------------------------------- administrators: dashboard and review
def _job_row(job: Job) -> dict:
    """One line of the dashboard: from the job's meta only (no document is loaded for a list)."""
    m = job.meta
    st = m.get("settings") or {}
    p = m.get("progress") or {}
    try:
        updated = (job.dir / "meta.json").stat().st_mtime
    except OSError:
        updated = m["created"]
    return {
        "id": job.id, "filename": m["filename"], "owner": m.get("owner"), "created": m["created"], "updated": updated,
        "status": m["status"], "running": runner.running(job.id), "error": m.get("error"),
        "pages": len(m["pages"]), "translatable": (m.get("analysis") or {}).get("translatable"),
        "done": p.get("done"), "total": p.get("total"), "selected": None if m.get("selected_pages") is None else len(m["selected_pages"]),
        "mode": st.get("mode"), "model": st.get("model_id"), "lang": st.get("target_lang"),
        "usage": m.get("usage") or {"requests": 0, "input": 0, "output": 0},
    }


@app.get("/api/admin/overview")
async def admin_overview():
    """Everything the dashboard shows at once: the members with what they used, and every document."""
    accounts.admin()
    jobs = [_job_row(j) for j in sorted(store.jobs.values(), key=lambda j: -j.meta["created"])]
    per: dict[str | None, dict] = {}
    for j in jobs:
        a = per.setdefault(j["owner"], {"documents": 0, "pages": 0, "input": 0, "output": 0, "requests": 0, "last": 0})
        a["documents"] += 1
        a["pages"] += j["pages"]
        for k in ("input", "output", "requests"):
            a[k] += int(j["usage"].get(k) or 0)
        a["last"] = max(a["last"], j["updated"])
    none = {"documents": 0, "pages": 0, "input": 0, "output": 0, "requests": 0, "last": 0}
    users = [{**u.public(), **per.get(u.id, none)} for u in await members.all_users()] if auth.ENABLED else []
    return {
        "users": users, "jobs": jobs,
        "totals": {
            "users": len(users), "blocked": sum(u["status"] == "blocked" for u in users), "documents": len(jobs),
            "pages": sum(j["pages"] for j in jobs), "running": sum(j["running"] for j in jobs),
            **{k: sum(int(j["usage"].get(k) or 0) for j in jobs) for k in ("input", "output", "requests")},
        },
    }


def _any_job(jid: str) -> Job:
    me = accounts.admin()
    job = store.get(jid)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    if not _mine(job):
        log.info("administrator %s is reviewing document %s (%s) of member %s", me.email, job.id, job.meta["filename"], job.meta.get("owner"))
    return job


@app.get("/api/admin/jobs/{jid}")
async def admin_job(jid: str):
    """A document for review: how it stands, whose it is, and how every page went."""
    job = _any_job(jid)
    owner = await members.get(job.meta["owner"]) if auth.ENABLED and job.meta.get("owner") else None
    pages = []
    if job.has_doc:
        doc = job.doc()
        states = pipeline.page_states(doc)
        frames: dict[int, int] = {}
        for b in doc["blocks"]:
            if b.get("kind") == "text":
                frames[b["page"]] = frames.get(b["page"], 0) + 1
        for i, size in enumerate(job.meta["pages"]):
            pg = (doc.get("pages") or {}).get(str(i)) or {}
            pages.append({"n": i, "w": size["w"], "h": size["h"], "state": states[i] if i < len(states) else 0,
                          "mode": pg.get("mode") or ("advanced" if pg else None), "model": pg.get("model"), "calls": pg.get("calls"),
                          "frames": frames.get(i, 0), "lost": len(pg.get("lost") or [])})
    return {**_job_row(job), "owner_user": owner.public() if owner else None, "page_list": pages,
            "warnings": (job.meta.get("analysis") or {}).get("warnings") or [],
            "compose": {k: (job.meta.get("compose") or {}).get(k) for k in ("version", "fit")}}


@app.get("/api/admin/jobs/{jid}/pages/{n}")
async def admin_page(jid: str, n: int):
    """One page for review, in PDF points of the ORIGINAL page (top-left origin): every frame with its source text and
    its translation, the text left as printed, what was lost, and Docling's regions when they were the hints."""
    job = _any_job(jid)
    if not 0 <= n < len(job.meta["pages"]):
        raise HTTPException(404)
    out = {"n": n, **job.meta["pages"][n], "frames": [], "excluded": [], "lost": [], "parser": [], "mode": None, "model": None,
           "calls": None, "hints": None, "done": False}
    if job.has_doc:
        doc = job.doc()
        for b in doc["blocks"]:
            if b.get("kind") == "text" and b.get("page") == n:
                out["frames"].append({"id": b["id"], "rect": b["bbox"], "src": b.get("src") or "", "tgt": b.get("tgt"),
                                      "chrome": bool(b.get("chrome")), "lines": b.get("n_lines", 1)})
        out["excluded"] = doc.get("untouched", {}).get(str(n), [])
        pg = doc.get("pages", {}).get(str(n)) or {}
        out.update(lost=pg.get("lost") or [], mode=pg.get("mode") or ("advanced" if pg else None), model=pg.get("model"),
                   calls=pg.get("calls"), hints=pg.get("hints"), done=pipeline.page_done(doc, n))
        if out["mode"] == "basic" and pg.get("hints"):
            out["parser"] = frames_basic.parser_boxes(pipeline.load_parse(job).get(n))
    return out


@app.get("/api/admin/policy")
async def get_policy():
    """The blanks of the privacy policy page that only the operator can fill in."""
    accounts.admin()
    return policy.public()


class PolicyPatch(BaseModel):
    values: dict[str, str]


@app.put("/api/admin/policy")
async def put_policy(p: PolicyPatch):
    me = accounts.admin()
    unknown = set(p.values) - policy.KEYS
    if unknown:
        raise HTTPException(400, f"알 수 없는 항목입니다: {', '.join(sorted(unknown))}")
    policy.save(p.values)
    log.info("privacy policy values changed by %s: %s", me.email, sorted(p.values))
    return policy.public()


@app.get("/api/health")
async def health():
    return {"ok": True}


_ASSET_RE = re.compile(r'(href|src)="/([\w.-]+\.(?:css|js))"')


def _page_html(name: str = "index.html") -> str | None:
    """A page of the frontend with the content hash of the stylesheet and the script in their URLs
    (`/app.js?v=3f9a1c20`): a browser or a CDN that kept an old copy (Cloudflare gives .css/.js a browser TTL of hours
    whatever the origin says) can never pair a new page with old files."""
    def tag(m: re.Match) -> str:
        try:
            v = hashlib.sha1((config.FRONTEND_DIR / m.group(2)).read_bytes()).hexdigest()[:10]
        except OSError:
            return m.group(0)
        return f'{m.group(1)}="/{m.group(2)}?v={v}"'
    try:
        page = _ASSET_RE.sub(tag, (config.FRONTEND_DIR / name).read_text("utf-8"))
    except OSError:
        return None
    return policy.render(page) if name == "privacy_policy.html" else page    # the operator's blanks (dashboard)


class _Frontend(StaticFiles):
    """The UI files change with every release. The pages themselves are always revalidated (`no-cache`); the files they
    name carry their content hash (see `_page_html`), so those may be kept for good."""

    async def get_response(self, path, scope):
        name = "index.html" if path in (".", "") else path
        if name.endswith(".html") and "/" not in name and "\\" not in name:
            html = _page_html(name)
            if html is not None:
                return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
        resp = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"") and resp.status_code == 200
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if versioned else "no-cache"
        return resp


# static frontend (must be last)
if config.FRONTEND_DIR.exists():
    app.mount("/", _Frontend(directory=config.FRONTEND_DIR, html=True), name="frontend")
