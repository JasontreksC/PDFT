"""Keeps `composed.pdf` + `positions.json` up to date for each job.

Edits (font / size / colour of a block) and new translations mark the job dirty; one background task per
job re-typesets (in a thread) and bumps `meta.compose.version`, which is how the viewer knows to reload.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from pathlib import Path

from ..config import SettingsStore
from ..jobs import Job
from ..pdf.fonts import FontManager
from ..frames.compose import ComposeError, compose

log = logging.getLogger("pdft.composer")


class ComposeManager:
    def __init__(self, fonts: FontManager, settings: SettingsStore):
        self.fonts = fonts
        self.settings = settings
        self._tasks: dict[str, asyncio.Task] = {}
        self._dirty: dict[str, float] = {}      # job id -> earliest time to run
        self._last: dict[str, float] = {}
        self._fit: dict[str, dict] = {}         # per job: page index -> (content signature, composed page bytes, hit boxes, stats)

    def request(self, job: Job, *, delay: float = 0.0, min_interval: float = 0.0) -> None:
        """Mark dirty. `min_interval` throttles repeated runs (used while translation is streaming in)."""
        if not job.has_doc:
            return
        now = time.time()
        when = max(now + delay, self._last.get(job.id, 0.0) + min_interval)
        self._dirty[job.id] = when if job.id not in self._dirty else min(self._dirty[job.id], when)
        c = job.meta.setdefault("compose", None) or {"version": 0, "pages": 0, "busy": False, "error": None}
        c["pending"] = True
        job.meta["compose"] = c
        if job.id not in self._tasks or self._tasks[job.id].done():
            self._tasks[job.id] = asyncio.create_task(self._loop(job))

    def forget(self, jid: str) -> None:
        """Drop everything kept for a deleted job (running task, page cache)."""
        t = self._tasks.pop(jid, None)
        if t and not t.done():
            t.cancel()
        for d in (self._dirty, self._last, self._fit):
            d.pop(jid, None)

    def idle(self, job: Job) -> bool:
        t = self._tasks.get(job.id)
        return not (t and not t.done()) and job.id not in self._dirty

    async def wait(self, job: Job) -> None:
        t = self._tasks.get(job.id)
        while t and not t.done():
            await asyncio.sleep(0.1)
            t = self._tasks.get(job.id)

    async def _loop(self, job: Job) -> None:
        while job.id in self._dirty:
            wait = self._dirty[job.id] - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            del self._dirty[job.id]
            c = job.meta["compose"]
            c["busy"], c["pending"] = True, job.id in self._dirty
            try:
                snapshot = copy.deepcopy(job.doc())      # the editor may keep changing the live doc
                default = self.settings.data.get("default_font")
                tmp = job.dir / "composed.next.pdf"
                flow = self.settings.data.get("layout_mode") == "flow"
                info = await asyncio.to_thread(compose, snapshot, self.fonts, default, tmp, job.source,
                                               self._fit.setdefault(job.id, {}), flow)
                tmp.replace(job.composed)
                job.set_positions(info["positions"])
                c.update(version=c.get("version", 0) + 1, pages=info["pages"], error=None,
                         page_size=info["page_size"], fit=info["fit"])
                self._clean_render_cache(job)
            except ComposeError as e:
                c["error"] = str(e)
                log.warning("compose failed for %s: %s", job.id, e)
            except Exception as e:  # noqa: BLE001
                c["error"] = f"조판 오류: {e}"
                log.exception("compose failed")
            finally:
                c["busy"] = False
                self._last[job.id] = time.time()
                c["pending"] = job.id in self._dirty
                job.save_meta()

    @staticmethod
    def _clean_render_cache(job: Job) -> None:
        for f in job.render_dir.glob("c*_*.png") if job.render_dir.exists() else []:
            if not f.name.startswith(f"c{job.meta['compose']['version']}_"):
                f.unlink(missing_ok=True)
