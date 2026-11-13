"""Compose on the ORIGINAL pages.

Each page is copied from the source PDF. Only the TEXT of the translated frames is removed (redaction with
images and line art kept), so rules, backgrounds, logos, pictures and vector diagrams stay exactly as printed.
The translation is then typeset into the frame's own place:

  1. its original box
  2. widened into the free space beside it (one-line frames) / lengthened into the free space below it
  3. otherwise the text is scaled down until it fits
That is all in the default mode ("place"): every translation stays where its source was printed, so it never drifts away
from what is left printed around it (list numbers, formulas, figure references). In the "flow" mode, frames that are alone
on their row are FLOWED instead: the space a shorter (longer) translation frees (needs) moves the frames below it, until
something printed there (a rule, a box, a picture, untranslated text) stops it.

"Free space" is decided only by geometry. Pages whose frames did not change are reused from a per-job cache.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import pymupdf

from ..compose import markup
from ..pdf import PYMU_LOCK
from ..pdf.fonts import FontManager
from . import geometry

MIN_SCALE = 0.55
MARGIN = 10.0


class ComposeError(RuntimeError):
    pass


# ------------------------------------------------------------------------------ geometry helpers
def _free_rect(f: dict, boxes: list[pymupdf.Rect], blockers, containers, W: float, H: float):
    """-> (box, widened, lengthened, both) free-space variants of the frame's box"""
    x0, y0, x1, y1 = f["bbox"]
    B = pymupdf.Rect(x0, y0, x1, y1)
    size = f["style"]["size"]
    probe = B + (-0.5, -0.5, 0.5, 0.5)
    cont = None
    for c in containers:
        if c.contains(probe) and (cont is None or c.get_area() < cont.get_area()):
            cont = c
    lim = pymupdf.Rect(MARGIN, MARGIN, W - MARGIN, H - MARGIN) if cont is None else pymupdf.Rect(cont.x0 + 1, cont.y0 + 1, cont.x1 - 1, cont.y1 - 1)
    others = [o for o in boxes if o != B] + [b for b in blockers if not (cont is not None and b == cont) and not b.contains(probe)]
    bottom = lim.y1
    for o in others:
        if o.y0 >= B.y1 - 1 and min(o.x1, B.x1) - max(o.x0, B.x0) > 1:
            bottom = min(bottom, o.y0 - 0.2 * size)
    bottom = max(bottom, B.y1)
    right, left = lim.x1, lim.x0
    for o in others:
        if min(o.y1, B.y1) - max(o.y0, B.y0) > 1:
            if o.x0 >= B.x1 - 1:
                right = min(right, o.x0 - 0.5 * size)
            elif o.x1 <= B.x0 + 1:
                left = max(left, o.x1 + 0.5 * size)
    right, left = max(right, B.x1), min(left, B.x0)
    align = f["align"]
    if align == "right":
        wide = pymupdf.Rect(left, B.y0, B.x1, B.y1)
    elif align == "center":
        half = min(B.x0 - left, right - B.x1)
        wide = pymupdf.Rect(B.x0 - half, B.y0, B.x1 + half, B.y1)
    else:
        wide = pymupdf.Rect(B.x0, B.y0, right, B.y1)
    tall = pymupdf.Rect(B.x0, B.y0, B.x1, bottom)
    return B, wide, tall, pymupdf.Rect(wide.x0, B.y0, wide.x1, bottom)


def _measure(body: str, width: float, css: str, archive) -> float:
    story = pymupdf.Story(html=body, user_css=css + "body{margin:0;padding:0}", archive=archive)
    _more, filled = story.place(pymupdf.Rect(0, 0, max(width, 5), 4000))
    return max(filled[3] - filled[1], 1.0)


def _movable(f: dict, frames: list[dict], untouched: list[list[float]], containers) -> bool:
    """Alone on its row (nothing printed beside it), not page chrome, not inside a box / table cell / picture.

    Text that stays printed on the frame's rows pins the frame, wherever it sits: beside the box, or inside it - the list
    number in front of a paragraph's first line ("6.   Applicability. This standard ...", the number left out of the
    translation) lies inside the paragraph's box because the following lines start under it. The printed text cannot move,
    so a frame that moved would leave it behind and run into it.
    """
    if f.get("chrome") or f.get("paint"):
        return False
    B = pymupdf.Rect(f["bbox"])
    probe = B + (-0.5, -0.5, 0.5, 0.5)
    if any(c.contains(probe) for c in containers):
        return False
    for o in (pymupdf.Rect(u) for u in untouched):
        if min(o.y1, B.y1) - max(o.y0, B.y0) > 1:              # printed text on the frame's rows: beside it or inside it
            return False
    for o in (pymupdf.Rect(g["bbox"]) for g in frames if g is not f):
        if min(o.y1, B.y1) - max(o.y0, B.y0) > 1 and (o.x0 >= B.x1 - 1 or o.x1 <= B.x0 + 1):
            return False
    return True


def _stack(mov: list[dict], plan: dict[str, dict], fixed: list[pymupdf.Rect], H: float) -> list[dict]:
    """Segments of consecutive movable frames with nothing fixed between them -> [{frames, tops, limit}].
    A segment's first frame keeps its place; each next frame keeps its ORIGINAL gap to the previous one, so a shorter
    text pulls the rest up and a longer one pushes it down - as far as the next fixed thing below (limit) allows."""
    items = sorted(mov, key=lambda f: (f["bbox"][1], f["bbox"][0]))
    segs: list[list[dict]] = []
    prev = None
    for f in items:
        B = pymupdf.Rect(f["bbox"])
        if prev is not None:
            P = pymupdf.Rect(prev["bbox"])
            cut = any(fb.y1 > P.y1 + 0.5 and fb.y0 < B.y0 - 0.5 and
                      (min(fb.x1, B.x1) - max(fb.x0, B.x0) > 0 or min(fb.x1, P.x1) - max(fb.x0, P.x0) > 0) for fb in fixed)
            if cut:
                segs.append([])
        if not segs:
            segs.append([])
        segs[-1].append(f)
        prev = f
    out: list[dict] = []
    for seg in segs:
        first, last = pymupdf.Rect(seg[0]["bbox"]), pymupdf.Rect(seg[-1]["bbox"])
        limit = H - MARGIN
        for fb in fixed:
            if fb.y0 >= last.y1 - 0.5 and min(fb.x1, last.x1) - max(fb.x0, last.x0) > 0:
                limit = min(limit, fb.y0 - 0.3 * seg[-1]["style"]["size"])
        hs = [max(plan[f["id"]]["h"], plan[f["id"]]["hmeas"]) for f in seg]
        gaps = [max(0.0, pymupdf.Rect(b["bbox"]).y0 - pymupdf.Rect(a["bbox"]).y1) for a, b in zip(seg, seg[1:])]
        total = first.y0 + sum(hs) + sum(gaps)
        s_ = 1.0
        if total > limit:
            s_ = max(0.0, (limit - first.y0 - sum(hs)) / sum(gaps)) if sum(gaps) > 0 else 0.0
            if s_ < 0.35:
                continue                               # no room to flow this segment: frames keep their own places
        tops, y = {}, first.y0
        for k, f in enumerate(seg):
            tops[f["id"]] = y
            y += hs[k] + (gaps[k] * s_ if k < len(gaps) else 0)
        out.append({"frames": seg, "tops": tops, "limit": limit, "gaps": gaps, "scale": s_})
    return out


# ------------------------------------------------------------------------------ what is to be drawn
def _text_of(f: dict) -> str:
    return f["tgt"] if f.get("tgt") is not None else f["src"]


def _is_todo(f: dict) -> bool:
    if f.get("tgt") is not None and f["tgt"] != f["src"]:
        return True
    return f.get("style") != f.get("orig_style")       # restyled in the editor


def _cut(f: dict) -> list[list[float]]:
    """The boxes whose text is erased: "cut" (frames.build.redaction_rects), else, in documents made before it existed,
    the line boxes."""
    return f.get("cut") or f["lines"]


def _page_sig(frames: list[dict], default_family: str | None, paint: bool, flow: bool = False) -> str:
    h = hashlib.sha1()
    h.update(json.dumps([default_family, paint, flow], ensure_ascii=False).encode())
    for f in frames:
        h.update(json.dumps([f["id"], f.get("tgt"), f["style"], f["bbox"]], ensure_ascii=False).encode())
    return h.hexdigest()


SAFE = 2.0          # text is laid out and drawn this much narrower than its box (measure and draw never disagree on a hair)
MIN_LAST = 0.35     # last resort scale: text is never dropped


def _inner(x0: float, x1: float, align: str) -> tuple[float, float]:
    if align == "right":
        return x0 + SAFE, x1
    if align == "center":
        return x0 + SAFE / 2, x1 - SAFE / 2
    return x0, x1 - SAFE


def _draw(page, rect: pymupdf.Rect, body: str, css: str, archive, scale_low: float = 1) -> tuple[float, float]:
    """-> (height actually used, scale) or (-1, scale) when it did not fit (nothing is drawn then)."""
    spare, scale = page.insert_htmlbox(rect, body, css=css, archive=archive, scale_low=scale_low)
    return (rect.height - spare if spare >= 0 else -1.0), scale


def _compose_page(src: pymupdf.Document, pno: int, fs: list[dict], todo: list[dict], untouched: list[list[float]],
                  paint: bool, key, faces: dict, css: str, archive, stats: dict,
                  flow: bool = False) -> tuple[bytes, dict[str, list[float]]]:
    pd = pymupdf.open()
    pd.insert_pdf(src, from_page=pno, to_page=pno)
    page = pd[0]
    W, H = page.rect.width, page.rect.height
    blockers, containers = geometry.obstacles(page, W, H)
    boxes = {f["id"]: pymupdf.Rect(f["bbox"]) for f in fs}          # where everything is printed NOW (updated as frames move)
    fixed_text = [pymupdf.Rect(b) for b in untouched]

    mov = [f for f in todo if flow and _movable(f, fs, untouched, containers)]     # "place" mode: nothing moves
    mov_ids = {f["id"] for f in mov}

    # ---- natural size of every movable frame (its own width, or the free space beside a one-liner)
    plan: dict[str, dict] = {}
    for f in todo:
        st = f["style"]
        size = float(st["size"])
        k = key(st)
        pitch = f["pitch"] * size / max(f.get("orig_size") or size, 1) if f.get("pitch") else None
        ratio = max(1.2, pitch / size if pitch else 1.25)
        base = faces[k]
        bold_face = faces[key(st, 300)] if markup.has_bold(_text_of(f)) else None
        text_html = markup.to_html(_text_of(f), bold_face)
        ind, ml = "", 0.0
        if f["n_lines"] > 1 and f["indent"]:
            ind = f"text-indent:{f['indent']:.1f}pt;"       # first-line indent (>0) or hanging bullet (<0)
            ml = max(0.0, -f["indent"])
        body = (f'<div style="font-family:\'{base}\';font-size:{size}pt;color:{st["color"]};line-height:{ratio:.2f};'
                f'text-align:{f["align"]};margin:0 0 0 {ml:.1f}pt;{ind}">{text_html}</div>')
        lh = pitch or size * ratio
        pl = {"body": body, "size": size, "lh": lh, "ratio": ratio}
        if f["id"] in mov_ids:
            B, wide, _t, _b = _free_rect(f, list(boxes.values()) + fixed_text, blockers, containers, W, H)
            x0, x1 = _inner(B.x0, B.x1 + 1, f["align"])
            h0 = _measure(body, x1 - x0, css, archive)
            h = h0
            if f["n_lines"] == 1 and wide.width > B.width + 1 and h0 > 1.4 * lh:
                wx0, wx1 = _inner(wide.x0, wide.x1, f["align"])
                h1 = _measure(body, wx1 - wx0, css, archive)
                if h1 < h0 - 1:
                    x0, x1, h = wx0, wx1, h1
            n_t = max(1, round(h / lh))
            h_layout = B.height + (n_t - f["n_lines"]) * lh if n_t != f["n_lines"] else B.height
            pl.update(hmeas=h, x0=x0, x1=x1, h=max(h_layout, 1.0), B=B)
        plan[f["id"]] = pl

    # ---- remove the old text (and, on picture-backed pages, paint its place)
    bgs = {}
    # "cut": the line boxes pulled back from the glyphs that stay printed (frames.build.redaction_rects); older documents
    # have only "lines"
    for f in todo:
        if paint or f.get("paint"):
            for lb in _cut(f):
                bgs[tuple(lb)] = geometry.background_color(page, pymupdf.Rect(lb))
    for f in todo:
        for lb in _cut(f):
            page.add_redact_annot(pymupdf.Rect(lb) + (0.4, 0.4, -0.4, -0.4), fill=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    n_ul = 0
    for d in page.get_drawings():           # link underlines are vector strokes under the text: they would stay behind
        r = pymupdf.Rect(d["rect"])
        if r.height > 1.6 or r.width < 4:
            continue
        if any(r.x0 >= lb[0] - 2 and r.x1 <= lb[2] + 2 and lb[3] - 3.5 <= r.y0 <= lb[3] + 3.5 for f in todo for lb in _cut(f)):
            page.add_redact_annot(r + (-0.4, -0.4, 0.4, 0.4), fill=False)
            n_ul += 1
    if n_ul:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
    for lb, bg in bgs.items():
        page.draw_rect(pymupdf.Rect(lb) + (-1, -1, 1, 1), color=None, fill=bg, overlay=True)

    pos: dict[str, list[float]] = {}
    drawn: set[str] = set()

    # ---- flow: draw the stacked frames top to bottom; a frame that needs more room than measured pushes the rest down
    fixed = [boxes[f["id"]] for f in fs if f["id"] not in mov_ids] + fixed_text + blockers
    for sg in _stack(mov, plan, fixed, H):
        delta = 0.0
        for f in sg["frames"]:
            pl = plan[f["id"]]
            size, lh = pl["size"], pl["lh"]
            top = sg["tops"][f["id"]] + delta
            planned = max(pl["h"], pl["hmeas"])
            avail = max(sg["limit"] - top, 0.0)               # never grow into what is printed below the segment
            used, last_h = None, -1.0
            for i, extra in enumerate((0.35 * size, 0.35 * size + lh, 0.35 * size + 2 * lh)):
                h = min(planned + extra, avail)
                if h <= last_h + 0.5:
                    break
                last_h = h
                u, _s = _draw(page, pymupdf.Rect(pl["x0"], top, pl["x1"], top + max(h, 1.0)), pl["body"], css, archive)
                if u >= 0:
                    used = planned + i * lh        # (the engine's "spare" is not a reliable text height: count whole extra lines)
                    break
            if used is None:                                    # does not fit at full size: shrink, but always draw
                rect = pymupdf.Rect(pl["x0"], top, pl["x1"], top + max(avail, planned * 0.6))
                u, sc = _draw(page, rect, pl["body"], css, archive, scale_low=MIN_SCALE)
                if u < 0:
                    u, sc = _draw(page, rect, pl["body"], css, archive, scale_low=MIN_LAST)
                stats["scaled" if u >= 0 else "failed"] += 1
                used = min(rect.height, planned)
            else:
                stats["flowed"] += 1
            delta += max(0.0, used - planned)
            if abs(top - pl["B"].y0) > 0.5:
                stats["moved"] += 1
                stats["pulled_pt"] += round(pl["B"].y0 - top)
            boxes[f["id"]] = pymupdf.Rect(pl["x0"], top, pl["x1"], top + used)
            pos[f["id"]] = [round(v, 1) for v in (pl["x0"], top, pl["x1"], top + used)]
            drawn.add(f["id"])
            stats["frames"] += 1

    # ---- everything else (beside other text, in boxes, page chrome, frames of a segment without room): in its own place
    for f in todo:
        if f["id"] in drawn:
            continue
        stats["frames"] += 1
        pl = plan[f["id"]]
        size = pl["size"]
        B, wide, tall, both = _free_rect(f, [b for i, b in boxes.items() if i != f["id"]] + fixed_text, blockers, containers, W, H)
        B = pymupdf.Rect(f["bbox"])
        bx0, bx1 = _inner(B.x0, B.x1 + 1, f["align"])
        wx0, wx1 = _inner(wide.x0, wide.x1, f["align"])
        pad = 0.3 * size
        tries = [("in_place", pymupdf.Rect(bx0, B.y0 - 1, bx1, B.y1 + pad))]
        if f["n_lines"] == 1 and wide.width > B.width + 1:
            tries.append(("widened", pymupdf.Rect(wx0, wide.y0 - 1, wx1, wide.y1 + pad)))
        if tall.height > B.height + 1:
            tries.append(("lengthened", pymupdf.Rect(bx0, tall.y0, bx1, tall.y1)))
        if f["n_lines"] == 1 and both.width > B.width + 1 and both.height > B.height + 1:
            tries.append(("both", pymupdf.Rect(wx0, both.y0, wx1, both.y1)))
        used_rect = None
        for name, rect in tries:
            u, _s = _draw(page, rect, pl["body"], css, archive)
            if u >= 0:
                stats[name] = stats.get(name, 0) + 1
                used_rect = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + u)
                break
        if used_rect is None:
            rect = tries[-1][1]
            u, _s = _draw(page, rect, pl["body"], css, archive, scale_low=MIN_SCALE)
            if u < 0:
                u, _s = _draw(page, rect, pl["body"], css, archive, scale_low=MIN_LAST)
            stats["scaled" if u >= 0 else "failed"] += 1
            used_rect = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + max(u, 1.0))
        boxes[f["id"]] = used_rect
        pos[f["id"]] = [round(v, 1) for v in used_rect]
    data = pd.tobytes(garbage=3, deflate=True)
    pd.close()
    return data, pos


def compose(doc: dict, fonts: FontManager, default_family: str | None, out_pdf: Path, source_pdf: Path,
            cache: dict | None = None, flow: bool = False) -> dict:
    """Write `out_pdf` (same pages as the source). Returns {"pages", "positions", "page_size", "fit"}.
    flow=False ("place", the default): every translation in its own place; True: free frames are flowed (see top)."""
    cache = cache if cache is not None else {}
    n = int(doc["page_count"])
    keep = set(doc.get("keep_pages") or [])
    paint_pages = set(doc.get("paint_pages") or [])
    by_page: dict[int, list[dict]] = {}
    for f in doc["blocks"]:
        by_page.setdefault(int(f["page"]), []).append(f)
    todo_by_page = {p: [f for f in fs if _is_todo(f)] for p, fs in by_page.items() if p not in keep}
    todo_by_page = {p: t for p, t in todo_by_page.items() if t}

    def key(style: dict, extra: int = 0) -> tuple[str, int]:
        fam = fonts.resolve(style.get("font"), default_family)
        if fam is None:
            raise ComposeError("사용할 수 있는 글꼴이 없습니다 (fonts 폴더 확인)")
        return fam, fonts.snap_weight(fam, min(900, int(style.get("weight", 400)) + extra))

    usage: dict[tuple[str, int], set[str]] = {}
    for fs in todo_by_page.values():
        for f in fs:
            t = _text_of(f)
            chars = set(markup.plain_text(t)) | set("•–◦ 0123456789.")
            usage.setdefault(key(f["style"]), set()).update(chars)
            if markup.has_bold(t):
                usage.setdefault(key(f["style"], 300), set()).update(chars)

    tmp = Path(tempfile.mkdtemp(prefix="pdft-compose-"))
    src = pymupdf.open(source_pdf)
    out = pymupdf.open()
    stats = {"frames": 0, "flowed": 0, "moved": 0, "pulled_pt": 0, "in_place": 0, "widened": 0, "lengthened": 0, "both": 0,
             "scaled": 0, "failed": 0}
    positions: dict[str, list[dict]] = {}
    try:
        faces: dict[tuple[str, int], str] = {}
        css = ""
        for i, (k, chars) in enumerate(usage.items()):
            path = fonts.materialize(k[0], k[1], chars, tmp)
            faces[k] = f"f{i}"
            css += f'@font-face{{font-family:"f{i}";src:url("{path.name}")}}\n'
        archive = pymupdf.Archive(str(tmp))
        for pno in range(n):
            todo = todo_by_page.get(pno)
            with PYMU_LOCK:                              # PyMuPDF is not thread-safe; the viewer renders pages meanwhile
                if not todo:
                    out.insert_pdf(src, from_page=pno, to_page=pno)
                    continue
                fs = by_page[pno]
                sig = _page_sig(todo, default_family, pno in paint_pages, flow)
                hit = cache.get(pno)
                if hit and hit[0] == sig:
                    data, pos, pst = hit[1], hit[2], hit[3]
                else:
                    pst = {k: 0 for k in stats}
                    data, pos = _compose_page(src, pno, fs, todo, doc.get("untouched", {}).get(str(pno), []),
                                              pno in paint_pages, key, faces, css, archive, pst, flow)
                    cache[pno] = (sig, data, pos, pst)
                for k, v in pst.items():
                    stats[k] = stats.get(k, 0) + v
                for fid, r in pos.items():
                    positions[fid] = [{"page": pno, "col": 0, "rect": r}]
                pd = pymupdf.open("pdf", data)
                out.insert_pdf(pd)
                pd.close()
        for pno, fs in by_page.items():                 # hit boxes for the frames that were not redrawn: where they were printed
            for f in fs:
                positions.setdefault(f["id"], [{"page": pno, "col": 0, "rect": [round(v, 1) for v in f["bbox"]]}])
        out.save(out_pdf, garbage=4, deflate=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        src.close()
        out.close()
    stats["compressed"] = stats["scaled"]
    stats["overflow"] = stats["failed"]
    return {"pages": n, "positions": positions, "page_size": list(doc["page_size"]), "fit": stats}
