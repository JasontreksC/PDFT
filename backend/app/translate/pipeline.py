"""Sequential, context-carrying translation of a document (paragraph units, in reading order)."""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Callable

from ..config import ModelEntry, SettingsStore
from ..jobs import Job
from ..llm import clients as llm
from ..llm import limits
from . import prompts
from .context import MAX_PAIR_CHARS, PAIR_EXCERPT, Context, pairs_text
from ..frames.build import needs_translation
from .tokens import estimate_tokens

log = logging.getLogger("pdft.pipeline")


# A request carries only: the system prompt, a SHORT background summary and the glossary entries that occur in the
# batch (plus the text to translate). Earlier source/translation pairs are not sent; they are collected in
# `Context.history` and folded into the summary (and glossary) every SUMMARY_EVERY tokens by one extra LLM call.
SUMMARY_EVERY = int(os.environ.get("PDFT_SUMMARY_EVERY", "6000"))      # unsummarised pair tokens that trigger a refresh
SUMMARY_TOKENS = int(os.environ.get("PDFT_SUMMARY_TOKENS", "700"))     # upper bound of the summary
GLOSSARY_TOKENS = int(os.environ.get("PDFT_GLOSSARY_TOKENS", "1500"))  # upper bound of the glossary sent per request
SUMMARY_FEED = int(os.environ.get("PDFT_SUMMARY_FEED", "16000"))       # most pair tokens one refresh reads
MAX_PENDING = 3 * SUMMARY_FEED                                          # buffer bound when refreshes are off or failing


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
    return max(200, min(GLOSSARY_TOKENS, int(limit * 0.1)))


def summary_budget(limit: int) -> int:
    return max(200, min(SUMMARY_TOKENS, int(limit * 0.05)))


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


async def compute_gauge(job: Job, entry: ModelEntry, lang: str) -> dict:
    """Numbers only. The content of the context is never exposed.
    used = what every request carries besides the text itself (system prompt + summary + glossary);
    pending = pair tokens waiting for the next summary refresh (`refresh_at`)."""
    limit, src = await limits.lookup(entry)
    ctx = job.context()
    used = ctx.used_tokens(prompts.translate_system(lang), glossary_budget(limit))
    return {
        "model_id": entry.id, "limit": limit, "limit_source": src, "used": used,
        "pending": ctx.pending_tokens(), "refresh_at": SUMMARY_EVERY, "compressions": ctx.compressions,
    }


def _fit_summary(text: str, budget: int) -> str:
    """The prompt asks for at most `budget` tokens; a model that ignores it must not inflate every later request."""
    cap = budget * 2
    if estimate_tokens(text) <= cap:
        return text
    cut = text[: int(cap * 3.2)]
    end = max(cut.rfind(". "), cut.rfind("\n"), cut.rfind("。"))
    return (cut[: end + 1] if end > len(cut) // 2 else cut).strip()


async def refresh_summary(ctx: Context, entry: ModelEntry, lang: str, limit: int, job: Job | None = None) -> bool:
    """Fold the pairs translated since the last refresh into the short background summary and the glossary.
    New pairs may keep arriving in `ctx.history` while the call runs; on failure/cancel the taken pairs are put back."""
    if not ctx.history:
        return False
    taken, ctx.history = ctx.history, []
    budget = summary_budget(limit)
    system = prompts.compress_system()
    user = prompts.compress_user(ctx.summary, pairs_text(taken, excerpt=PAIR_EXCERPT, max_tokens=SUMMARY_FEED), lang, budget)
    try:
        res = await llm.chat(entry, system, user, cache_key=f"pdft-{job.id}" if job else None)
    except asyncio.CancelledError:
        ctx.history = taken + ctx.history
        raise
    except llm.LLMError as e:
        log.warning("summary refresh failed (%s); the pairs stay queued", e)
        ctx.history = taken + ctx.history
        ctx.trim_pending(MAX_PENDING)
        return False
    meter(job, res)
    ctx.calibrate(estimate_tokens(system) + estimate_tokens(user), res.prompt_tokens)
    summary = prompts.parse_summary(res.text)
    ctx.add_terms(prompts.parse_terms(res.text))
    if not summary:                                   # unusable answer: keep the old summary, drop the pairs (terms were kept)
        log.warning("summary refresh returned no summary")
        return False
    ctx.summary = _fit_summary(summary, budget)
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

    def start(self, job: Job, entry: ModelEntry, lang: str, auto: bool, concurrency: int = 1) -> None:
        job.meta["settings"] = {**job.meta.get("settings", {}), "model_id": entry.id, "target_lang": lang,
                                "auto_compress": auto, "concurrency": concurrency}
        job.meta["status"] = "translating"
        job.meta["error"] = None
        job.save_meta()
        self.tasks[job.id] = asyncio.create_task(self._run(job, entry, lang, auto, max(1, min(16, int(concurrency)))))

    async def cancel(self, jid: str) -> None:
        t = self.tasks.get(jid)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ main loop
    async def _run(self, job: Job, entry: ModelEntry, lang: str, auto: bool, conc: int = 1) -> None:
        meta = job.meta
        ctx = job.context()
        refresh: asyncio.Task | None = None
        running: dict[asyncio.Task, int] = {}
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
            # (summary + glossary) as it is when the page is started, translates on its own copy, and its new pairs /
            # terms are merged back when it finishes. The summary is refreshed by a separate call that runs next to
            # the pages (a running page keeps the snapshot it started with, so nothing changes under a request).
            pending = deque(groups)
            sem = asyncio.Semaphore(conc) if conc > 1 else None      # requests in flight, over all pages
            finished = 0
            hold = 0                                                  # after a failed refresh wait for a few pages
            try:
                while pending or running:
                    if auto and refresh is None and finished >= hold and ctx.pending_tokens() >= SUMMARY_EVERY:
                        refresh = asyncio.create_task(refresh_summary(ctx, entry, lang, limit, job))
                    while pending and len(running) < conc:
                        g = pending.popleft()
                        page_no = g[0].page + 1
                        t = asyncio.create_task(self._page_worker(job, entry, ctx, lang, limit, g, page_no, page_total, sem))
                        running[t] = page_no
                        pages = sorted(running.values())
                        prog["message"] = (f"{pages[0]}/{page_total} 쪽 번역 중" if len(pages) == 1
                                           else f"{pages[0]}–{pages[-1]}/{page_total} 쪽 번역 중 (동시 {len(pages)}쪽)")
                    waiting: set[asyncio.Task] = set(running)
                    if refresh is not None:
                        waiting.add(refresh)
                    done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                    for t in done:
                        if t is refresh:
                            refresh = None
                            ok = False
                            try:
                                ok = t.result()
                            except Exception:  # noqa: BLE001 - a failed summary must never stop the translation
                                log.exception("summary refresh crashed")
                            if not ok and ctx.history:
                                hold = finished + 5
                            job.save_context()
                            continue
                        page_no = running.pop(t)
                        local, sink = t.result()           # re-raises the worker's error
                        self._merge(ctx, local, sink, page_no, auto)
                        finished += 1
                        prog["done"] = sum(1 for u in units if u.ref.get("tgt") is not None)
                        job.save_doc()
                        job.save_context()
                        job.save_meta()
                        self.on_progress(job, False)
                if refresh is not None:                        # let the last refresh land so it is saved with the job
                    prog["message"] = "맥락 요약 정리 중"
                    try:
                        await refresh
                    except Exception:  # noqa: BLE001
                        log.exception("summary refresh crashed")
                    refresh = None
            finally:
                live = list(running) + ([refresh] if refresh is not None else [])
                for t in live:
                    t.cancel()
                if live:
                    await asyncio.gather(*live, return_exceptions=True)
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

    async def _page_worker(self, job, entry, ctx: Context, lang, limit, group: list[Unit], page_no, page_total,
                           sem: asyncio.Semaphore | None = None):
        local = ctx.snapshot()                         # the summary + glossary this page works from
        sink = {"pairs": [], "terms": [], "memo": {}}
        await self._translate_group(job, entry, local, lang, limit, group, page_no, page_total, sink, sem)
        return local, sink

    @staticmethod
    def _merge(ctx: Context, local: Context, sink: dict, page_no: int, auto: bool = True) -> None:
        """Fold what one finished page learned back into the shared context."""
        if sink["pairs"]:
            ctx.history.append({"page": page_no, "pairs": sink["pairs"]})
        ctx.memo.update(sink["memo"])
        ctx.add_terms(sink["terms"])
        ctx.calib = local.calib
        if not auto:                                   # nobody will summarise: keep the buffer bounded
            ctx.trim_pending(MAX_PENDING)

    async def _translate_group(self, job, entry, ctx, lang, limit, group: list[Unit], page_no, page_total,
                               sink: dict, sem: asyncio.Semaphore | None = None) -> None:
        """One page. With `sem` (parallel mode) its batches are sent concurrently from the page's snapshot of the
        context (at most as many requests in flight, over all pages, as the semaphore allows); without it they are
        sent one after the other and each batch sees the terms learned from the previous one."""
        system = prompts.translate_system(lang)
        gbudget = glossary_budget(limit)
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
            items = [(i, u.kind, u.src) for i, u in enumerate(batch)]
            result: dict[int, str] = {}
            for _attempt in range(3):                      # a retry only has to fill the gaps
                remaining = [(i, k, t) for i, k, t in items if i not in result]
                if not remaining:
                    break
                glossary = ctx.glossary_text(" ".join(t for _, _, t in remaining), gbudget)
                user = prompts.translate_user(ctx.summary, glossary, page_no, page_total, remaining)
                if sem is not None:
                    async with sem:
                        res = await llm.chat(entry, system, user, cache_key=f"pdft-{job.id}")
                else:
                    res = await llm.chat(entry, system, user, cache_key=f"pdft-{job.id}")
                meter(job, res)
                ctx.calibrate(estimate_tokens(system) + estimate_tokens(user), res.prompt_tokens)
                terms = prompts.parse_terms(res.text)
                ctx.add_terms(terms)
                sink["terms"].extend(terms)
                for i, text in prompts.parse_translations(res.text).items():
                    if text:
                        result[i] = text
            for i, u in enumerate(batch):
                if i in result:
                    u.set(result[i])
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
            sink["pairs"].append([u.src[:200], (u.ref["tgt"] or "")[:200]])
