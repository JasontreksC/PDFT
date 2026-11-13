"""Page-by-page translation of a document, in one of two modes.

"advanced" (고급 번역, a vision model): one request per page - the screenshot of the page plus the instructions (and the
short background summary / glossary of the job) go to the model, which answers with the text worth translating as
{lines (the printed text, line by line), translation} items. The lines are matched against the page's text layer to find
where each text sits (frames.build.frames_from_items); the translations become the frames' text.

"basic" (기본 번역, any text model): the document is analysed by the Docling service once (its regions are cached in
parse.json), the frames of every page are built from the text layer with those regions as hints (frames.basic), and the
text of the frames goes to the model in batches of <t> elements - no screenshot.

Pages are translated up to `concurrency` at a time in both modes; the summary / glossary memory is shared.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from collections import deque
from typing import Callable

import pymupdf

from ..config import ModelEntry, SettingsStore
from ..jobs import Job
from ..llm import clients as llm
from ..llm import limits
from ..llm import vision
from ..pdf import PYMU_LOCK, render
from . import prompts
from .context import MAX_PAIR_CHARS, PAIR_EXCERPT, Context, pairs_text
from ..frames import basic as frames_basic
from ..frames import build as frames_build
from ..parse import docling_client
from ..structure.textlayer import strip_markup
from ..match.llm_json import parse_page
from ..match.matcher import norm_llm
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


MODES = ("basic", "advanced")


def system_prompt(lang: str, mode: str) -> str:
    return prompts.text_system(lang) if mode == "basic" else prompts.translate_system(lang)


async def compute_gauge(job: Job, entry: ModelEntry, lang: str, mode: str = "advanced") -> dict:
    """Numbers only. The content of the context is never exposed.
    used = what every request carries besides the text itself (system prompt + summary + glossary);
    pending = pair tokens waiting for the next summary refresh (`refresh_at`)."""
    limit, src = await limits.lookup(entry)
    ctx = job.context()
    used = ctx.used_tokens(system_prompt(lang, mode), glossary_budget(limit))
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



MAX_CALLS = 4          # model calls for one page: retries of an unreadable answer + continuations of a cut-off one


# ------------------------------------------------------------------------------ page state
def page_done(doc: dict, i: int) -> bool:
    """Translated: the page has been through the vision translation, or (a job made by an older version) all of its
    frames that need a translation have one."""
    if (doc.get("pages") or {}).get(str(i), {}).get("done"):
        return True
    blocks = [b for b in doc["blocks"] if b["page"] == i and frames_build.needs_translation(b["src"])]
    return bool(blocks) and all(b.get("tgt") is not None for b in blocks)


def page_states(doc: dict) -> list[int]:
    """Per page: 0 not translated, 1 partly, 2 translated, 3 nothing to translate (cover, scan, rotated)."""
    can = frames_build.translatable_pages(doc)
    out = []
    for i in range(doc["page_count"]):
        if i not in can:
            out.append(3)
        elif page_done(doc, i):
            out.append(2)
        else:
            tgt = [b for b in doc["blocks"] if b["page"] == i and b.get("tgt") is not None and frames_build.needs_translation(b["src"])]
            out.append(1 if tgt else 0)
    return out


def pages_to_do(doc: dict, selection: set[int] | None, *, only_open: bool = True) -> list[int]:
    can = frames_build.translatable_pages(doc)
    pick = [i for i in sorted(can) if selection is None or i in selection]
    return [i for i in pick if not (only_open and page_done(doc, i))]


def apply_page(job: Job, i: int, built: dict, extra: dict) -> bool:
    """Put the frames of page i into the document (replacing any it had) and remember how the page went.
    Returns whether the page counts as translated: the model found nothing to translate, or at least one of its items was
    found in the text layer. A page where the model gave items but none could be placed stays open (retry with 'continue')."""
    doc = job.doc()
    rep = built["report"]
    done = rep["frames"] > 0 or rep["items"] == 0
    doc["blocks"] = sorted([b for b in doc["blocks"] if b["page"] != i] + built["frames"], key=lambda b: b["page"])
    doc.setdefault("untouched", {})[str(i)] = built["untouched"]
    doc.setdefault("pages", {})[str(i)] = {"done": done, **rep, **extra}
    return done


def _read(job: Job, i: int):
    with PYMU_LOCK:
        return frames_build.read_page(job.source, i)


def _shot(job: Job, i: int, long_side: int) -> bytes:
    return render.render_png_long(job.source, i, long_side)


# ------------------------------------------------------------------------------ "basic": Docling's regions
def load_parse(job: Job) -> dict[int, dict]:
    """Docling's result per page index, as far as it has been analysed ({} when never). Reads the format of older versions
    (the whole document as {"items": {"pages": [{page_number, success, items}]}}) as well."""
    try:
        data = json.loads(job.parse_path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if "pages" in data and isinstance(data["pages"], dict):
        return {int(k): v for k, v in data["pages"].items()}
    return {p["page_number"] - 1: p for p in ((data.get("items") or {}).get("pages") or []) if "page_number" in p}


def _save_parse(job: Job, pages: dict[int, dict]) -> None:
    job.parse_path.write_text(json.dumps({"version": 1, "pages": {str(k): v for k, v in sorted(pages.items())}},
                                         ensure_ascii=False), "utf-8")


def _excerpt(job: Job, pages: list[int]) -> Path:
    """A PDF of only these pages (in this order): Docling analyses what is needed, not the whole book."""
    fd, name = tempfile.mkstemp(suffix=".pdf", prefix="pdft-dl-", dir=job.dir)
    os.close(fd)
    with PYMU_LOCK, pymupdf.open(job.source) as d:
        d.select(pages)
        d.save(name, garbage=3, deflate=True)
    return Path(name)


async def analyse(job: Job, pages: list[int], say: Callable[[str], None]) -> tuple[dict[int, dict], str | None]:
    """Docling's regions of `pages` (cached: only pages never analysed are sent). -> (regions per page, warning or None).
    Without the service the pages get no hints: they are still grouped by the typographic rules. Layout only: no OCR."""
    parsed = load_parse(job)
    missing = [i for i in pages if i not in parsed]
    if not missing:
        return parsed, None
    h = await docling_client.health()
    if not h or h.get("ok") is False:
        why = (h or {}).get("error") or "서비스에 연결할 수 없음"
        return parsed, f"Docling을 쓸 수 없어 레이아웃 힌트 없이 글상자를 묶었습니다 ({why})"
    tmp = await asyncio.to_thread(_excerpt, job, missing)
    try:
        res = await docling_client.parse_pdf_docling(tmp, on_status=lambda m: say(f"Docling 분석: {m}"))
    except docling_client.ParseError as e:
        return parsed, f"Docling 분석 실패, 레이아웃 힌트 없이 글상자를 묶었습니다: {e}"
    finally:
        tmp.unlink(missing_ok=True)
    for p in (res.get("items") or {}).get("pages") or []:
        k = p.get("page_number", 0) - 1
        if 0 <= k < len(missing):
            parsed[missing[k]] = {"success": p.get("success", True), "items": p.get("items") or []}
    _save_parse(job, parsed)
    return parsed, None


def make_batches(frames: list[dict], max_chars: int, max_items: int = 40) -> list[list[dict]]:
    batches: list[list[dict]] = []
    cur: list[dict] = []
    chars = 0
    for f in frames:
        n = len(f["src"])
        if cur and (chars + n > max_chars or len(cur) >= max_items):
            batches.append(cur)
            cur, chars = [], 0
        cur.append(f)
        chars += n
    if cur:
        batches.append(cur)
    return batches


class Runner:
    def __init__(self, settings: SettingsStore, on_progress: Callable[[Job, bool], None] | None = None):
        self.settings = settings
        self.tasks: dict[str, asyncio.Task] = {}
        self.on_progress = on_progress or (lambda job, final: None)   # -> ask the composer for a fresh layout

    def running(self, jid: str) -> bool:
        t = self.tasks.get(jid)
        return bool(t and not t.done())

    def start(self, job: Job, entry: ModelEntry, lang: str, auto: bool, concurrency: int = 1, mode: str = "advanced") -> None:
        job.meta["settings"] = {**job.meta.get("settings", {}), "model_id": entry.id, "target_lang": lang,
                                "auto_compress": auto, "concurrency": concurrency, "mode": mode}
        job.meta["status"] = "translating"
        job.meta["error"] = None
        job.save_meta()
        self.tasks[job.id] = asyncio.create_task(self._run(job, entry, lang, auto, max(1, min(16, int(concurrency))), mode))

    async def cancel(self, jid: str) -> None:
        t = self.tasks.get(jid)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ main loop
    async def _run(self, job: Job, entry: ModelEntry, lang: str, auto: bool, conc: int = 1, mode: str = "advanced") -> None:
        meta = job.meta
        ctx = job.context()
        refresh: asyncio.Task | None = None
        running: dict[asyncio.Task, int] = {}
        try:
            limit, _ = await limits.lookup(entry)
            doc = job.doc()
            sel = job.selection()
            can = frames_build.translatable_pages(doc)
            scope = can if sel is None else can & sel
            todo = pages_to_do(doc, sel)
            total = len(scope)
            prog = meta["progress"] = {"done": total - len(todo), "total": total, "message": "번역 시작",
                                       "warnings": meta.get("progress", {}).get("warnings", 0)}
            page_total = doc.get("page_count", 1)
            prebuilt: dict[int, dict] = {}
            if mode == "basic" and todo:              # Docling's regions (once per page, cached), then the frames of the pages
                prog["message"] = "Docling 레이아웃 분석 준비 중"
                parsed, warn = await analyse(job, todo, lambda m: prog.__setitem__("message", m))
                if warn:
                    log.warning("job %s: %s", job.id, warn)
                    prog["notice"] = warn
                    prog["warnings"] = prog.get("warnings", 0) + 1
                prog["message"] = "글상자 정리 중"
                prebuilt = await asyncio.to_thread(_build_basic, job, parsed, set(todo))
            # Pages are translated up to `conc` at a time. Every page starts from a snapshot of the shared context
            # (summary + glossary) as it is when the page is started, translates on its own copy, and its new pairs /
            # terms are merged back when it finishes. The summary is refreshed by a separate call that runs next to
            # the pages (a running page keeps the snapshot it started with, so nothing changes under a request).
            pending = deque(todo)
            sem = asyncio.Semaphore(conc)                             # model requests in flight, over all pages
            finished = 0
            hold = 0                                                  # after a failed refresh wait for a few pages
            try:
                while pending or running:
                    if auto and refresh is None and finished >= hold and ctx.pending_tokens() >= SUMMARY_EVERY:
                        refresh = asyncio.create_task(refresh_summary(ctx, entry, lang, limit, job))
                    while pending and len(running) < conc:
                        i = pending.popleft()
                        if mode == "basic":
                            t = asyncio.create_task(self._page_worker_basic(job, entry, ctx, lang, limit, i, page_total, sem,
                                                                            prebuilt.get(i), conc))
                        else:
                            t = asyncio.create_task(self._page_worker(job, entry, ctx, lang, limit, i, page_total, sem))
                        running[t] = i + 1
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
                        res = t.result()                   # re-raises the worker's error (key / model / network problems)
                        if res["built"] is not None:
                            done_page = apply_page(job, page_no - 1, res["built"], res["extra"])
                            self._merge(ctx, res["local"], res["sink"], page_no, auto)
                            prog["done"] += 1 if done_page else 0
                            lost = res["built"]["report"]["lost_n"]
                            if lost or res["extra"].get("truncated") or not done_page:
                                prog["warnings"] = prog.get("warnings", 0) + 1
                        else:
                            prog["warnings"] = prog.get("warnings", 0) + 1
                            log.warning("page %d: no usable answer from the model", page_no)
                        finished += 1
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
            meta["status"] = "done" if prog["done"] >= prog["total"] else "paused"
            prog["message"] = "번역 완료" if meta["status"] == "done" else "일부 쪽은 번역문을 놓을 자리를 찾지 못했습니다 (이어서 번역으로 다시 시도)"
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

    @staticmethod
    def _merge(ctx: Context, local: Context, sink: dict, page_no: int, auto: bool = True) -> None:
        """Fold what one finished page learned back into the shared context."""
        if sink["pairs"]:
            ctx.history.append({"page": page_no, "pairs": sink["pairs"]})
        ctx.add_terms(sink["terms"])
        ctx.memo.update(sink.get("memo") or {})
        ctx.calib = local.calib
        if not auto:                                   # nobody will summarise: keep the buffer bounded
            ctx.trim_pending(MAX_PENDING)

    # ------------------------------------------------------------------ one page
    async def _page_worker(self, job: Job, entry: ModelEntry, shared: Context, lang: str, limit: int, i: int, page_total: int,
                           sem: asyncio.Semaphore) -> dict:
        """screenshot -> model (retry an unreadable answer, continue a cut-off one) -> items -> frames.
        Returns {"built": frames/report or None, "extra", "local", "sink"}; applying it to the document is the caller's job."""
        ctx = shared.snapshot()                        # the summary + glossary this page works from
        sink = {"pairs": [], "terms": []}
        page_no = i + 1
        pi = await asyncio.to_thread(_read, job, i)
        png = await asyncio.to_thread(_shot, job, i, int(self.settings.data.get("image_long_side") or 2000))
        system = prompts.translate_system(lang)
        glossary = ctx.glossary_text(pi.text, glossary_budget(limit))
        items: list[dict] = []
        seen: set[str] = set()
        resume: str | None = None
        truncated = False
        ok = False
        calls = 0
        while calls < MAX_CALLS:
            user = prompts.translate_user(ctx.summary, glossary, page_no, page_total, resume)
            async with sem:
                res = await vision.chat_with_image(entry, system, user, png, cache_key=f"pdft-{job.id}")
            calls += 1
            meter(job, res)
            ctx.calibrate(estimate_tokens(system) + estimate_tokens(user), res.prompt_tokens)
            got, terms, warn, parsed = parse_page(res.text)
            fresh = [it for it in got if norm_llm(it["original"]) not in seen]
            seen.update(norm_llm(it["original"]) for it in fresh)
            items.extend(fresh)
            ctx.add_terms(terms)
            sink["terms"].extend(terms)
            if res.truncated and fresh:                 # cut off by the length limit: ask for the rest
                resume = fresh[-1]["original"][-80:]
                truncated = True
                continue
            if res.truncated or (not parsed and not items):    # nothing usable: ask again
                truncated = truncated or res.truncated
                continue
            ok = True
            truncated = False
            break
        if not ok and not items:
            return {"built": None, "extra": {}, "local": ctx, "sink": sink}
        paint = i in set(job.doc().get("paint_pages") or [])
        built = await asyncio.to_thread(frames_build.frames_from_items, pi, items, paint=paint,
                                        threshold=float(os.environ.get("PDFT_MATCH_THRESHOLD", frames_build.MATCH_THRESHOLD)))
        for it in items:
            if it.get("translation"):
                sink["pairs"].append([it["original"][:MAX_PAIR_CHARS], it["translation"][:MAX_PAIR_CHARS]])
        return {"built": built, "extra": {"model": entry.id, "calls": calls, "truncated": truncated and not ok, "mode": "advanced"}, "local": ctx, "sink": sink}

    # ------------------------------------------------------------------ one page, "basic"
    async def _page_worker_basic(self, job: Job, entry: ModelEntry, shared: Context, lang: str, limit: int, i: int,
                                 page_total: int, sem: asyncio.Semaphore, built: dict | None, conc: int) -> dict:
        """The frames of the page (built from the text layer + Docling's regions) -> their text to the model in batches of
        <t> elements. A batch is asked again (up to 3 times) for what the answer left out; what never comes back keeps its
        original text. One page at a time (conc 1): each batch sees the terms the previous one learned."""
        ctx = shared.snapshot()
        sink: dict = {"pairs": [], "terms": [], "memo": {}}
        page_no = i + 1
        if built is None:                               # no frames could be made for the page: it stays open
            return {"built": None, "extra": {}, "local": ctx, "sink": sink}
        system = prompts.text_system(lang)
        gbudget = glossary_budget(limit)
        max_chars = int(max(600, min(6000, limit * 0.12)))
        units = [f for f in built["frames"] if f.get("tgt") is None]
        pending: list[dict] = []
        for f in units:                                 # identical short strings (running headers, labels) are not sent again
            if f["src"] in ctx.memo:
                f["tgt"] = ctx.memo[f["src"]]
                sink["pairs"].append([f["src"][:200], f["tgt"][:200]])
            else:
                pending.append(f)
        calls = 0

        async def do_batch(batch: list[dict]) -> list[dict]:
            nonlocal calls
            items = [(k, f.get("role") or "text", f["src"]) for k, f in enumerate(batch)]
            result: dict[int, str] = {}
            for _attempt in range(3):                      # a retry only has to fill the gaps
                remaining = [(k, r, t) for k, r, t in items if k not in result]
                if not remaining:
                    break
                glossary = ctx.glossary_text(" ".join(t for _, _, t in remaining), gbudget)
                user = prompts.text_user(ctx.summary, glossary, page_no, page_total, remaining)
                async with sem:
                    res = await llm.chat(entry, system, user, cache_key=f"pdft-{job.id}")
                calls += 1
                meter(job, res)
                ctx.calibrate(estimate_tokens(system) + estimate_tokens(user), res.prompt_tokens)
                terms = prompts.parse_terms(res.text)
                ctx.add_terms(terms)
                sink["terms"].extend(terms)
                for k, text in prompts.parse_translations(res.text).items():
                    if text and 0 <= k < len(batch):
                        result[k] = text
            missed = []
            for k, f in enumerate(batch):
                if k in result:
                    f["tgt"] = result[k]
                    sink["pairs"].append([f["src"][:MAX_PAIR_CHARS], result[k][:MAX_PAIR_CHARS]])
                    if len(f["src"]) <= 200:
                        sink["memo"][f["src"]] = result[k]
                else:                                       # never returned: keep the original text rather than losing it
                    f["tgt"] = f["src"]
                    missed.append(f)
            return missed

        batches = make_batches(pending, max_chars)
        if conc > 1:
            missed = [f for m in await asyncio.gather(*(do_batch(b) for b in batches)) for f in m]
        else:
            missed = []
            for b in batches:
                missed.extend(await do_batch(b))
        rep = built["report"]
        rep["lost"] = [{"original": strip_markup(f["src"])[:160], "score": None, "reason": "모델이 번역문을 돌려주지 않음"} for f in missed][:40]
        rep["lost_n"] = len(missed)
        return {"built": built, "extra": {"model": entry.id, "calls": calls, "mode": "basic"}, "local": ctx, "sink": sink}


def _build_basic(job: Job, parsed: dict[int, dict], pages: set[int]) -> dict[int, dict]:
    with PYMU_LOCK:
        return frames_basic.build_pages(job.source, parsed, pages)
