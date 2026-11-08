"""Filesystem-backed job store.

jobs/<id>/
  meta.json               public job state (status, analysis info, progress, compose state)
  source.pdf              the upload
  parse.json              parser result of the local Docling service (items tree) - only when it was used
  doc.json                the document model (version 2): frames = pieces of text at their place on the original
                          page (src / tgt / style / geometry), plus untouched text, verbatim pages ...
  composed.pdf            the original pages with the translated text typeset in (viewer + export)
  positions.json          where every frame landed in composed.pdf (hit boxes for the editor)
  render/                 cached page PNGs
  internal/context.json   PRIVATE translation memory (summary / glossary) - never served
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

import pymupdf

from .translate.context import Context


def _write_json(path: Path, data, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":") if indent is None else None, indent=indent), "utf-8")
    tmp.replace(path)


class Job:
    def __init__(self, root: Path, meta: dict):
        self.dir = root
        self.meta = meta
        self._ctx: Context | None = None
        self._doc: dict | None = None
        self._positions: dict | None = None
        self.deleted = False                # a deleted job must never write its files again (background tasks may still run)

    @property
    def id(self) -> str:
        return self.meta["id"]

    source = property(lambda self: self.dir / "source.pdf")
    parse_path = property(lambda self: self.dir / "parse.json")
    doc_path = property(lambda self: self.dir / "doc.json")
    composed = property(lambda self: self.dir / "composed.pdf")
    positions_path = property(lambda self: self.dir / "positions.json")
    render_dir = property(lambda self: self.dir / "render")

    # ---------------------------------------------------------------- meta
    def save_meta(self) -> None:
        if self.deleted:
            return
        _write_json(self.dir / "meta.json", self.meta)

    def public(self) -> dict:
        m = self.meta
        return {
            "id": m["id"], "filename": m["filename"], "status": m["status"], "error": m.get("error"),
            "created": m["created"], "pages": m["pages"], "progress": m.get("progress"),
            "settings": m.get("settings", {}), "analysis": m.get("analysis"), "compose": m.get("compose"), "usage": m.get("usage"),
            "scan_pages": m.get("scan_pages", 0), "selected_pages": m.get("selected_pages"),
        }

    # ---------------------------------------------------------------- page selection
    def selection(self) -> set[int] | None:
        """Pages the user works on (translate / reset / partial export). None = all pages."""
        sel = self.meta.get("selected_pages")
        return None if sel is None else {int(i) for i in sel}

    def set_selection(self, pages: list[int] | None) -> None:
        n = len(self.meta["pages"])
        if pages is not None:
            pages = sorted({int(i) for i in pages if 0 <= int(i) < n})
            if len(pages) == n:
                pages = None
        self.meta["selected_pages"] = pages

    # ---------------------------------------------------------------- document model
    @property
    def has_doc(self) -> bool:
        return self.doc_path.exists()

    def doc(self) -> dict:
        if self._doc is None:
            self._doc = json.loads(self.doc_path.read_text("utf-8"))
        return self._doc

    def set_doc(self, doc: dict) -> None:
        self._doc = doc
        self.save_doc()

    def save_doc(self) -> None:
        if self._doc is not None and not self.deleted:
            _write_json(self.doc_path, self._doc)

    def block(self, bid: str) -> dict | None:
        return next((b for b in self.doc()["blocks"] if b["id"] == bid), None)

    def positions(self) -> dict:
        if self._positions is None:
            try:
                self._positions = json.loads(self.positions_path.read_text("utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                self._positions = {}
        return self._positions

    def set_positions(self, pos: dict) -> None:
        self._positions = pos
        if self.deleted:
            return
        _write_json(self.positions_path, pos)

    # ---------------------------------------------------------------- private translation memory
    def context(self) -> Context:
        if self._ctx is None:
            self._ctx = Context.load(self.dir / "internal" / "context.json")
        return self._ctx

    def save_context(self) -> None:
        if self.deleted:
            return
        self.context().save(self.dir / "internal" / "context.json")

    def reset_context(self) -> None:
        (self.dir / "internal" / "context.json").unlink(missing_ok=True)
        self._ctx = None


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        for d in root.iterdir():
            mf = d / "meta.json"
            if not mf.exists():
                continue
            try:
                meta = json.loads(mf.read_text("utf-8"))
            except json.JSONDecodeError:
                continue
            st = meta.get("status")
            if st == "translating":
                meta["status"] = "paused"
            elif st == "analyzing":   # the container restarted mid-analysis
                meta["status"] = "ready" if (d / "doc.json").exists() else "uploaded"
                meta["error"] = "분석 중 서버가 재시작되었습니다. 다시 분석하세요."
            dp = d / "doc.json"
            if dp.exists():
                try:
                    ver = json.loads(dp.read_text("utf-8")).get("version")
                except (json.JSONDecodeError, OSError):
                    ver = None
                if ver != 2:    # the page-flow document model of an earlier PDFT: keep the file, analyse again
                    dp.replace(d / "doc.v1.json")
                    for old_f in ("composed.pdf", "positions.json"):
                        (d / old_f).unlink(missing_ok=True)
                    meta.update(status="uploaded", analysis=None, compose=None,
                                error="이전 버전(쪽 흐름 방식)에서 만든 작업입니다. 새 방식으로 다시 분석하세요 (텍스트 층이 있으면 무료).")
                    meta["progress"] = {"done": 0, "total": 0, "message": "", "warnings": 0}
                    st = "uploaded"
            if st not in ("uploaded", "analyzing") and not (d / "doc.json").exists():
                # created by an older PDFT (page-box pipeline): its data is kept, but it must be analysed again
                meta.update(status="uploaded", analysis=None, compose=None,
                            error="이전 버전에서 만든 작업입니다. 분석 등급을 고르고 다시 분석하세요.")
                meta["progress"] = {"done": 0, "total": 0, "message": "", "warnings": 0}
            if "compose" in meta and meta["compose"]:
                meta["compose"]["busy"] = False
            self.jobs[meta["id"]] = Job(d, meta)

    def create(self, filename: str) -> Job:
        jid = uuid.uuid4().hex[:12]
        d = self.root / jid
        d.mkdir(parents=True)
        meta = {
            "id": jid, "filename": filename, "status": "uploaded", "error": None, "created": time.time(),
            "pages": [], "progress": {"done": 0, "total": 0, "message": "", "warnings": 0},
            "settings": {}, "analysis": None, "compose": None, "scan_pages": 0,
        }
        job = Job(d, meta)
        self.jobs[jid] = job
        return job

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def list(self) -> list[dict]:
        items = sorted(self.jobs.values(), key=lambda j: -j.meta["created"])
        out = []
        for j in items:
            doc_blocks = None
            out.append({
                "id": j.id, "filename": j.meta["filename"], "status": j.meta["status"], "created": j.meta["created"],
                "pages": len(j.meta["pages"]),
                "progress": j.meta.get("progress"),
            })
        return out

    def delete(self, jid: str) -> bool:
        job = self.jobs.pop(jid, None)
        if not job:
            return False
        job.deleted = True
        shutil.rmtree(job.dir, ignore_errors=True)
        return True


def page_sizes(pdf: Path) -> list[dict]:
    with pymupdf.open(pdf) as d:
        return [{"w": round(p.rect.width, 2), "h": round(p.rect.height, 2)} for p in d]
