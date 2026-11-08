"""Sequential, context-carrying translation of a document (paragraph units, in reading order)."""
from __future__ import annotations

import asyncio
import copy
import logging
import os
from collections import deque
from typing import Callable

from ..config import ModelEntry, SettingsStore
from ..jobs import Job
from ..llm import clients as llm
from ..llm import limits
from . import prompts
from .context import MAX_PAIR_CHARS, Context
from ..frames.build import needs_translation
from .tokens import estimate_tokens

log = logging.getLogger("pdft.pipeline")


# Every request re-sends the standing context (summary + glossary + raw recent pairs), so its size is the
# cost driver. It is capped independently of the model's window: with a 1M-token model and "compress at 70%"
# the raw history would grow to 700k tokens and be re-sent with every request (a 288-page book cost ~10^8 tokens).
CONTEXT_CAP = int(os.environ.get("PDFT_CONTEXT_CAP", "24000"))


def trigger_for(limit: int, pct: int) -> int:
    """Context size at which the history is compressed."""
    return int(min(limit * pct / 100, CONTEXT_CAP))


def meter(job: Job | None, res) -> None:
    """Add the provider-reported token usage of one call to the job's running totals."""
    if job is None:
        return
    u = job.meta.setdefault("usage", {"requests": 0, "input": 0, "output": 0})
    u["requests"] += 1
    u["cached"] = u.get("cached", 0) + int(res.cached_tokens or 0)
    u["input"] += int(res.prompt_tokens or 0)
    u["output"] += int(res.completion_tokens or 0)


def glossary_budget(limit: int) -> int:
    return max(200, min(4000, int(limit * 0.1)))


class Unit:
    """One thing to translate: a text frame. Writes the result back in place (and into identical running
    headers/footers of other pages, which are translated once)."""
    __slots__ = ("ref", "kind", "page", "peers")

    def __init__(self, ref: dict, kind: str, page: int):
        self.ref, self.kind, self.page = ref, kind, page
        self.peers: list[dict] = []

    @property
    def src(self) -> str:
        return self.ref["src"]

    def set(self, text: str | None) -> None:
        self.ref["tgt"] = text
        for p in self.peers:
            p["tgt"] = text


def collect_units(doc: dict, pages: set[int] | None = None, reuse: bool = False) -> list[Unit]:
    """Units in reading order (translated ones included). A page header/footer that repeats verbatim is ONE unit
    covering its frames on the selected pages. `pages` restricts the result to those pages (None = whole document);
    frames outside the selection are never touched. `reuse=True` (translation) copies a header/footer translation
    that exists elsewhere to the selected frames instead of translating it again."""
    out: list[Unit] = []
    groups: dict[str, list[dict]] = {}
    for b in doc["blocks"]:
        if b.get("chrome") and needs_translation(b["src"]):
            groups.setdefault(b["src"], []).append(b)
    placed: dict[str, Unit] = {}
    for b in doc["blocks"]:
        inside = pages is None or b["page"] in pages
        if b.get("chrome") and needs_translation(b["src"]):
            if not inside or b["src"] in placed:
                continue
            members = [g for g in groups[b["src"]] if pages is None or g["page"] in pages]
            u = Unit(members[0], b.get("role") or "header", members[0]["page"])
            u.peers = members[1:]
            known = next((g["tgt"] for g in groups[b["src"]] if g.get("tgt") is not None), None)
            if reuse and known is not None:           # already translated somewhere: reuse it for the selected pages
                for g in members:
                    g["tgt"] = known
            placed[b["src"]] = u
            out.append(u)
        elif inside:
            out.append(Unit(b, b.get("role") or "text", b["page"]))
    return out


def make_batches(units: list[Unit], max_chars: int, max_items: int = 40) -> list[list[Unit]]:
    batches: list[list[Unit]] = []
    cur: list[Unit] = []
    chars = 0
    for u in units:
        n = len(u.src)
        if cur and (chars + n > max_chars or len(cur) >= max_items):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(u)
        chars += n
    if cur:
        batches.append(cur)
    return batches


async def compute_gauge(job: Job, entry: ModelEntry, pct: int, lang: str) -> dict:
    """Numbers only. The content of the context is never exposed."""
    limit, src = await limits.lookup(entry)
    ctx = job.context()
    used = ctx.used_tokens(prompts.translate_system(lang), glossary_budget(limit))
    return {
        "model_id": entry.id, "limit": limit, "limit_source": src, "used": used,
        "trigger": trigger_for(limit, pct), "pct": pct, "cap": CONTEXT_CAP, "compressions": ctx.compressions,
    }


async def compress_context(ctx: Context, entry: ModelEntry, lang: str, limit: int, job: Job | None = None) -> bool:
    """Fold the raw pairs (+ old summary) into a new, bounded summary."""
    if not ctx.history and not ctx.cur_pairs:
        return False
    budget = max(300, min(3000, int(limit * 0.05)))
    system, user = prompts.compress_system(), prompts.compress_user(
        ctx.summary, ctx.history_text(), lang, budget)
    try:
        res = await llm.chat(entry, system, user, cache_key=f"pdft-{job.id}" if job else None)
        meter(job, res)
        ctx.calibrate(estimate_tokens(system) + estimate_tokens(user), res.prompt_tokens)
        summary = prompts.parse_summary(res.text)
        ctx.add_terms(prompts.parse_terms(res.text))
    except llm.LLMError as e:
        log.warning("context compression failed (%s); trimming instead", e)
        summary = ""
    if summary:
        ctx.summary = summary
        ctx.history, ctx.cur_pairs = [], []
    else:
        if ctx.cur_pairs:
            ctx.history.append({"page": None, "pairs": ctx.cur_pairs})
            ctx.cur_pairs = []
        ctx.hard_trim(0.3)
    ctx.compressions += 1
    return True


class Runner:
    def __init__(self, settings: SettingsStore, on_progress: Callable[[Job, bool], None] | None = None):
        self.settings = settings
        self.tasks: dict[str, asyncio.Task] = {}
        self.on_progress = on_progress or (lambda job, final: None)   # -> ask the composer for a fresh layout

    def running(self, jid: str) -> bool:
        t = self.tasks.get(jid)
        return bool(t and not t.done())

    def start(self, job: Job, entry: ModelEntry, lang: str, auto: bool, pct: int, concurrency: int = 1) -> None:
        job.meta["settings"] = {**job.meta.get("settings", {}), "model_id": entry.id, "target_lang": lang,
                                "auto_compress": auto, "compress_pct": pct, "concurrency": concurrency}
        job.meta["status"] = "translating"
        job.meta["error"] = None
        job.save_meta()
        self.tasks[job.id] = asyncio.create_task(self._run(job, entry, lang, auto, pct, max(1, min(16, int(concurrency)))))

    async def cancel(self, jid: str) -> None:
        t = self.tasks.get(jid)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ main loop
    async def _run(self, job: Job, entry: ModelEntry, lang: str, auto: bool, pct: int, conc: int = 1) -> None:
        meta = job.meta
        ctx = job.context()
        try:
            limit, _ = await limits.lookup(entry)
            doc = job.doc()
            units = collect_units(doc, job.selection(), reuse=True)
            total = len(units)
            prog = meta["progress"] = {"done": sum(1 for u in units if u.ref.get("tgt") is not None), "total": total,
                                       "message": "번역 시작", "warnings": meta.get("progress", {}).get("warnings", 0)}
            page_total = doc.get("page_count", 1)
            # one request group per original page, in reading order
            groups: list[list[Unit]] = []
            for u in units:
                if u.ref.get("tgt") is not None:
                    continue
                if groups and groups[-1][0].page == u.page:
                    groups[-1].append(u)
                else:
                    groups.append([u])
            # Pages are translated up to `conc` at a time. Every page starts from a snapshot of the shared context
            # (summary, earlier pairs, glossary) as it is when the page is started, translates on its own copy, and its
            # new pairs / terms are merged back when it finishes. Compression happens only while nothing is running
            # (the summary must not change under a request that is in flight).
            system = prompts.translate_system(lang)
            gbudget = glossary_budget(limit)
            trigger = trigger_for(limit, pct)
            max_chars = int(max(600, min(6000, limit * 0.12)))
            pending = deque(groups)
            running: dict[asyncio.Task, int] = {}
            sem = asyncio.Semaphore(conc) if conc > 1 else None      # requests in flight, over all pages
            try:
                while pending or running:
                    while pending and len(running) < conc:
                        g = pending[0]
                        est = max((sum(estimate_tokens(u.src) for u in b) * ctx.calib * 2.3 + 60
                                   for b in make_batches(g, max_chars)), default=0)
                        need = ctx.used_tokens(system, gbudget) + est
                        if (auto and need > trigger) or need > limit * 0.92:
                            if running:
                                break                      # drain first, then compress
                            prog["message"] = "컨텍스트 압축 중"
                            await compress_context(ctx, entry, lang, limit, job)
                            job.save_context()
                        pending.popleft()
                        page_no = g[0].page + 1
                        t = asyncio.create_task(self._page_worker(job, entry, ctx, lang, auto, pct, limit, g, page_no, page_total, sem))
                        running[t] = page_no
                        pages = sorted(running.values())
                        prog["message"] = (f"{pages[0]}/{page_total} 쪽 번역 중" if len(pages) == 1
                                           else f"{pages[0]}–{pages[-1]}/{page_total} 쪽 번역 중 (동시 {len(pages)}쪽)")
                    if not running:
                        continue
                    done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        page_no = running.pop(t)
                        local, sink = t.result()           # re-raises the worker's error
                        self._merge(ctx, local, sink, page_no)
                        prog["done"] = sum(1 for u in units if u.ref.get("tgt") is not None)
                        job.save_doc()
                        job.save_context()
                        job.save_meta()
                        self.on_progress(job, False)
            finally:
                for t in running:
                    t.cancel()
                if running:
                    await asyncio.gather(*running, return_exceptions=True)
            meta["status"] = "done"
            prog["message"] = "번역 완료"
            self.on_progress(job, True)
        except asyncio.CancelledError:
            meta["status"] = "paused"
            meta["progress"]["message"] = "일시 중지됨"
            self.on_progress(job, True)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("translation failed")
            meta["status"] = "error"
            meta["error"] = str(e)
        finally:
            try:
                job.save_doc()
                job.save_context()
                job.save_meta()
            except Exception:  # noqa: BLE001
                log.exception("could not persist job state")

    async def _page_worker(self, job, entry, ctx: Context, lang, auto, pct, limit, group: list[Unit], page_no, page_total,
                           sem: asyncio.Semaphore | None = None):
        local = copy.deepcopy(ctx)                     # the snapshot this page works from
        local.cur_pairs, local.cur_page = [], page_no
        sink = {"pairs": [], "terms": [], "memo": {}}
        await self._translate_group(job, entry, local, lang, auto, pct, limit, group, page_no, page_total, sink, sem)
        return local, sink

    @staticmethod
    def _merge(ctx: Context, local: Context, sink: dict, page_no: int) -> None:
        """Fold what one finished page learned back into the shared context."""
        if sink["pairs"]:
            ctx.history.append({"page": page_no, "pairs": sink["pairs"]})
        ctx.memo.update(sink["memo"])
        ctx.add_terms(sink["terms"])
        ctx.calib = local.calib

    async def _translate_group(self, job, entry, ctx, lang, auto, pct, limit, group: list[Unit], page_no, page_total,
                               sink: dict | None = None, sem: asyncio.Semaphore | None = None) -> None:
        """One page. With `sem` (parallel mode) its batches are sent concurrently from the page's snapshot of the
        context (at most as many requests in flight, over all pages, as the semaphore allows); without it they are
        sent one after the other and each batch sees the pairs of the previous one."""
        system = prompts.translate_system(lang)
        gbudget = glossary_budget(limit)
        trigger = trigger_for(limit, pct)
        max_chars = int(max(600, min(6000, limit * 0.12)))
        warnings = 0
        parallel = sem is not None

        # identical short strings (headers, repeated labels, "Yes"/"No" cells) are reused, not re-sent
        pending, reused = [], []
        for u in group:
            if u.src in ctx.memo:
                u.set(ctx.memo[u.src])
                reused.append(u)
            else:
                pending.append(u)

        async def do_batch(batch: list[Unit]) -> int:
            missed = 0
            est_batch = sum(estimate_tokens(u.src) for u in batch) * ctx.calib * 2.3 + 60
            if not parallel:
                for _ in range(2):
                    need = ctx.used_tokens(system, gbudget) + est_batch
                    if (auto and need > trigger) or need > limit * 0.92:
                        job.meta["progress"]["message"] = "컨텍스트 압축 중"
                        await compress_context(ctx, entry, lang, limit, job)
                        job.meta["progress"]["message"] = f"{page_no}/{page_total} 쪽 번역 중"
                    else:
                        break
            items = [(i, u.kind, u.src) for i, u in enumerate(batch)]
            result: dict[int, str] = {}
            for _attempt in range(3):
                remaining = [(i, k, t) for i, k, t in items if i not in result]
                if not remaining:
                    break
                glossary = ctx.glossary_text(" ".join(t for _, _, t in remaining), gbudget)
                # a retry only has to fill the gaps: do not re-send the (large) raw history again
                recent = ctx.history_text() if _attempt == 0 else ""
                if parallel and ctx.used_tokens(system, gbudget) + est_batch > limit * 0.92:
                    recent = ""                            # (parallel pages never compress mid-flight: leave the raw pairs out)
                user = prompts.translate_user(ctx.summary, glossary, recent, page_no, page_total, remaining)
                if sem is not None:
                    async with sem:
                        res = await llm.chat(entry, system, user, cache_key=f"pdft-{job.id}")
                else:
                    res = await llm.chat(entry, system, user, cache_key=f"pdft-{job.id}")
                meter(job, res)
                ctx.calibrate(estimate_tokens(system) + estimate_tokens(user), res.prompt_tokens)
                terms = prompts.parse_terms(res.text)
                ctx.add_terms(terms)
                if sink is not None:
                    sink["terms"].extend(terms)
                for i, text in prompts.parse_translations(res.text).items():
                    if text:
                        result[i] = text
            for i, u in enumerate(batch):
                if i in result:
                    u.set(result[i])
                    ctx.add_pair(u.src, result[i])
                    if sink is not None:
                        sink["pairs"].append([u.src[:MAX_PAIR_CHARS], result[i][:MAX_PAIR_CHARS]])
                        if len(u.src) <= 200:
                            sink["memo"][u.src] = result[i]
                else:   # the model never returned it: keep the original text rather than losing it
                    u.set(u.src)
                    missed += 1
            return missed

        batches = make_batches(pending, max_chars)
        if parallel:
            warnings = sum(await asyncio.gather(*(do_batch(b) for b in batches)))
        else:
            for batch in batches:
                warnings += await do_batch(batch)
        job.meta["progress"]["warnings"] = job.meta["progress"].get("warnings", 0) + warnings
        for u in reused:
            ctx.cur_pairs.append([u.src[:200], (u.ref["tgt"] or "")[:200]])
            if sink is not None:
                sink["pairs"].append([u.src[:200], (u.ref["tgt"] or "")[:200]])
