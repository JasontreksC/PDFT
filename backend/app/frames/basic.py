"""Frames for "기본 번역": the text layer grouped by typography, with the regions of Docling's layout analysis as hints.

This is the earlier way of finding the translation units (before the vision translation, "고급 번역"):

  * Docling first: all text-layer lines (and pieces of lines) whose centre lies in the same Docling region are ONE frame,
    whatever the typography says - side-by-side pieces of a row, lines of different size or indent, a label and its text.
    Tables are not recognised (table structure is off) and neither table areas nor the per-line items Docling reports in
    them are used, so the text of a table is grouped by the rules below; so are pictures, page headers / footers.
  * the lines no region holds (no Docling, or outside every usable region) are grouped by typography alone:
    - candidate link: the lower line sits right below, similar size, and shares an edge / the centre line
    - wrap test (the typographic rule itself): a line break inside a paragraph exists because the NEXT word did not fit -
      so the upper line must be filled up to the text block's right edge within one word
    - explicit markers (bullet glyphs, "1."), a bold<->regular change or a colon label start a new frame

Nothing is learned from the document (no measured leading): every decision looks at the two lines themselves, so a short
excerpt behaves like the whole book. A wrong decision only moves a frame boundary; the text stays where it was printed.
The text of every frame goes to a TEXT model (no screenshot); what is in it is decided here, not by the model - formulas
and numbers in a paragraph are sent along with it.

Pages without a text layer (scans) are not translated: nothing is read by OCR, Docling only analyses the layout.

`build_pages(pdf, parsed, pages)` -> {page: {"frames", "untouched", "report"}} in the same shape as
frames.build.frames_from_items, so a page is put into the document the same way whichever mode translated it.
"""
from __future__ import annotations

import re
from collections import Counter

import pymupdf

from ..structure import textlayer as tl
from ..structure.reading_order import xy_cut
from .build import _finish, infer_alignment_style, make_frame, needs_translation, redaction_rects, scan_pages_of, verbatim_pages

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _tokens(l: tl.Line) -> int:
    t = l.plain
    return len(t.split()) if not _CJK.search(t) else max(len(t.split()), len(t) // 4)


def _first_word_w(l: tl.Line) -> float:
    t = l.plain
    if _CJK.search(t[:1]):
        return l.size
    m = re.match(r"\S+", t)
    return (l.bbox[2] - l.bbox[0]) * (len(m.group(0)) if m else 1) / max(len(t), 1)


def _align_link(a: tl.Line, b: tl.Line, a_is_head: bool) -> str | None:
    tol = 0.5 * max(a.size, b.size)
    dl = a.bbox[0] - b.bbox[0]
    if abs(dl) <= tol:
        return "left"
    if a_is_head and tol < dl <= 4 * a.size:
        return "left"                                  # first-line indent
    if a.marker and 0.3 * a.size < -dl <= 3 * a.size:
        return "left"                                  # hanging indent under a bullet
    if abs(a.bbox[2] - b.bbox[2]) <= tol:
        return "right"
    if abs(a.cx - b.cx) <= tol:
        return "center"
    return None


def candidate_chains(lines: list[tl.Line]) -> list[list[tuple[tl.Line, str | None]]]:
    """Lines linked into candidate chains by position/size alone (no judgement about wrapping yet)."""
    order = sorted(range(len(lines)), key=lambda i: (lines[i].bbox[1], lines[i].bbox[0]))
    pred: dict[int, tuple[int, str]] = {}
    succ: dict[int, int] = {}
    for bi in order:
        b = lines[bi]
        best = None
        for ai in order:
            a = lines[ai]
            if ai == bi or a.cy >= b.cy - 0.4 * min(a.height, b.height):
                continue
            if ai in succ:
                continue
            gap = b.bbox[1] - a.bbox[3]
            if gap > 1.0 * max(a.size, b.size) or abs(a.size - b.size) > 0.12 * max(a.size, b.size):
                continue
            if min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0]) <= 0:
                continue
            al = _align_link(a, b, a_is_head=ai not in pred)
            if al is None:
                continue
            pitch = b.base - a.base
            if best is None or pitch < best[0]:
                best = (pitch, ai, al)
        if best:
            pred[bi] = (best[1], best[2])
            succ[best[1]] = bi
    chains = []
    for i in order:
        if i in pred:
            continue
        ch = [(lines[i], None)]
        j = i
        while j in succ:
            k = succ[j]
            ch.append((lines[k], pred[k][1]))
            j = k
        chains.append(ch)
    return chains


def _bounds(chain) -> tuple[float, float]:
    return min(l.bbox[0] for l, _ in chain), max(l.bbox[2] for l, _ in chain)


def split_chain(chain):
    """Cut a candidate chain into frames (lines no Docling region holds)."""
    L, R = _bounds(chain)
    groups = [[chain[0][0]]]
    for (a, _), (b, al) in zip(chain, chain[1:]):
        cut = False
        if b.marker:
            cut = True
        elif re.search(r"[:：]$", a.plain):
            cut = True                                  # "Cost:" - a label never wraps into the next line
        elif abs(a.bold_share - b.bold_share) > 0.6:
            cut = True
        else:
            hyph = a.plain.endswith("-") and len(a.plain) > 2
            if al == "right":
                slack = a.bbox[0] - L
            elif al == "center":
                slack = (R - L) - (a.bbox[2] - a.bbox[0])
            else:
                slack = R - a.bbox[2]
            fits_next = slack > _first_word_w(b) + 0.28 * a.size + 0.25 * a.size
            if (not hyph and fits_next) or _tokens(a) < 2:
                cut = True
        if cut:
            groups.append([b])
        else:
            groups[-1].append(b)
    return groups


def doc_repeated(doc: pymupdf.Document, page_lines: dict[int, list[tl.Line]]) -> set[str]:
    """Running headers / footers: the same line (digits ignored) near the top or bottom of at least half the pages."""
    n = doc.page_count
    if n < 3:
        return set()
    seen: Counter = Counter()
    for i, ls in page_lines.items():
        H = doc[i].rect.height
        seen.update({re.sub(r"\d+", "#", l.plain.lower()) for l in ls if l.cy < 0.12 * H or l.cy > 0.88 * H})
    return {t for t, c in seen.items() if c >= max(3, 0.5 * n) and t.strip()}


def _rects(items, out: list[dict], depth: int = 0, in_list: bool = False, hints: bool = False) -> None:
    """Parser (Docling) items -> flat list of text-ish regions {kind, rect, md}.
    hints=True: regions used only as 'these lines belong to different boxes' hints - the per-LINE items Docling reports
    inside a table (table structure off) are left out there, they would cut every line of a paragraph."""
    for it in items or []:
        if hints and it.get("in_table"):
            continue
        t = it.get("type")
        if t in ("header", "footer", "image", "table"):
            continue
        if t == "list":
            _rects(it.get("items"), out, depth + 1, True, hints)
            continue
        bbs = [b for b in (it.get("bbox") or []) if b.get("w", 0) > 0 and b.get("h", 0) > 0]
        md = (it.get("md") or it.get("value") or "").strip()
        if not bbs or not md:
            continue
        kind = "heading" if t == "heading" else ("code" if t == "code" else ("list_item" if in_list else "text"))
        rect = [min(b["x"] for b in bbs), min(b["y"] for b in bbs), max(b["x"] + b["w"] for b in bbs), max(b["y"] + b["h"] for b in bbs)]
        out.append({"kind": kind, "rect": rect, "md": md, "level": it.get("level"), "line": bool(it.get("in_table"))})


def parser_boxes(page: dict | None) -> list[dict]:
    """The raw regions of one parsed page, for the analysis view: [{kind, rect}]."""
    out: list[dict] = []

    def walk(items):
        for it in items or []:
            if it.get("type") == "list":
                walk(it.get("items"))
                continue
            for b in it.get("bbox") or []:
                if b.get("w", 0) > 0 and b.get("h", 0) > 0:
                    out.append({"kind": b.get("label") or it.get("type") or "text",
                                "rect": [round(b["x"], 1), round(b["y"], 1), round(b["x"] + b["w"], 1), round(b["y"] + b["h"], 1)]})
    walk((page or {}).get("items"))
    return out


def _region_of(regions: list[dict], line: tl.Line) -> int | None:
    """The smallest region that holds the centre of the line (2 pt of slack)."""
    best, area = None, 1e18
    for k, r in enumerate(regions):
        x0, y0, x1, y1 = r["rect"]
        if x0 - 2 <= line.cx <= x1 + 2 and y0 - 2 <= line.cy <= y1 + 2 and (x1 - x0) * (y1 - y0) < area:
            best, area = k, (x1 - x0) * (y1 - y0)
    return best


def _copy(l: tl.Line, runs: list[tl.Run] | None = None) -> tl.Line:
    new = tl.Line(l.page, list(l.bbox), l.size, l.color, runs if runs is not None else [tl.Run(r.text, r.bold, r.italic, r.url) for r in l.runs],
                  l.bold_share, l.marker, l.bbox_idx, l.tag, l.base)
    return new


def _join_row(row: list[tl.Line]) -> tl.Line:
    """Pieces of one printed row that belong to the same region (a number and its title, cells the PDF split): one line,
    left to right, its box spanning them all."""
    if len(row) == 1:
        return row[0]
    row = sorted(row, key=lambda l: l.bbox[0])
    runs: list[tl.Run] = []
    for k, l in enumerate(row):
        if k:
            runs.append(tl.Run(" "))
            if l.marker and l.marker[0] == "bullet":
                runs.append(tl.Run(l.marker[1] + " "))
        runs.extend(tl.Run(r.text, r.bold, r.italic, r.url) for r in l.runs)
    n = [max(1, len(l.plain)) for l in row]
    dom = row[n.index(max(n))]
    new = _copy(row[0], runs)
    new.bbox = [min(l.bbox[0] for l in row), min(l.bbox[1] for l in row), max(l.bbox[2] for l in row), max(l.bbox[3] for l in row)]
    new.size, new.color, new.base = dom.size, dom.color, dom.base
    new.bold_share = sum(l.bold_share * w for l, w in zip(row, n)) / sum(n)
    new.pieces = [list(l.bbox) for l in row]          # what is erased: the pieces, not the gap between them
    return new


def region_groups(lines: list[tl.Line], regions: list[dict]) -> tuple[list[list[tl.Line]], list[tl.Line]]:
    """-> (one list of frame lines per Docling region that holds any line, the lines no region holds).
    The lines of a region are put in reading order and the pieces that share a row are joined into one line. A bullet that
    starts a later line of the region stays in front of its text (only the first line's bullet is kept by make_frame)."""
    by: dict[int, list[tl.Line]] = {}
    rest: list[tl.Line] = []
    for l in lines:
        k = _region_of(regions, l) if regions else None
        (by.setdefault(k, []) if k is not None else rest).append(l)
    groups: list[list[tl.Line]] = []
    for k in sorted(by):
        ls = sorted(by[k], key=lambda l: (l.cy, l.bbox[0]))
        rows: list[list[tl.Line]] = []
        for l in ls:
            if rows and abs(rows[-1][0].cy - l.cy) <= 0.5 * min(l.height, rows[-1][0].height):
                rows[-1].append(l)
            else:
                rows.append([l])
        joined = [_join_row(r) for r in rows]
        for j in range(1, len(joined)):
            l = joined[j]
            if l.marker and l.marker[0] == "bullet":
                joined[j] = _copy(l, [tl.Run(l.marker[1] + " ")] + [tl.Run(r.text, r.bold, r.italic, r.url) for r in l.runs])
                joined[j].marker = None
                if hasattr(l, "pieces"):
                    joined[j].pieces = l.pieces
        groups.append(joined)
    return groups, rest


def _pop_sure(frames: list[dict]) -> None:
    for f in frames:
        f.pop("_sure", None)


def _weighted_median(c: Counter) -> float | None:
    if not c:
        return None
    total = sum(c.values())
    acc = 0
    for size in sorted(c):
        acc += c[size]
        if acc >= total / 2:
            return size
    return None


def _role(f: dict, body: float) -> str:
    """A HINT for the translator only (it never decides placement)."""
    src = tl.strip_markup(f["src"])
    if f["style"]["size"] >= 1.25 * body and len(src) < 140:
        return "heading"
    if re.match(r"^([●○•◦▪■□▶►‣∙◆◇]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])\s", src):
        return "list_item"
    return "text"


def build_pages(pdf, parsed: dict[int, dict] | None, pages: set[int]) -> dict[int, dict]:
    """Frames of the given pages. `parsed`: Docling's result per page index ({"items": [...], "success": bool}) or None
    (no hints: the typographic rules alone). Scanned pages (no text layer) get no frames."""
    with pymupdf.open(pdf) as doc:
        return _build(doc, parsed or {}, pages)


def _build(doc: pymupdf.Document, parsed: dict[int, dict], pages: set[int]) -> dict[int, dict]:
    n = doc.page_count
    parsed = {i: p for i, p in parsed.items() if p.get("success", True)}
    keep = verbatim_pages(doc)
    scans = set(scan_pages_of(doc, keep))
    # the whole document is read for what only the whole can tell: the body text size and the running headers / footers
    all_lines: dict[int, list[tl.Line]] = {}
    for i in range(n):
        all_lines[i] = [] if (i in keep or i in scans or doc[i].rotation) else tl.page_lines(doc[i], i)
    sizes: Counter = Counter()
    for ls in all_lines.values():
        for l in ls:
            sizes[round(l.size, 1)] += len(l.plain)
    body = _weighted_median(sizes) or 10.0
    repeated = doc_repeated(doc, all_lines)

    out: dict[int, dict] = {}
    for i in sorted(pages):
        if not 0 <= i < n or i in keep or i in scans or doc[i].rotation:
            continue
        page = doc[i]
        W, H = page.rect.width, page.rect.height
        hints = i in parsed
        pf: list[dict] = []
        untouched: list[list[float]] = []
        lines = all_lines[i]
        if lines:                                          # (an empty page simply has no frames)
            imgs = []
            try:
                imgs = [pymupdf.Rect(inf["bbox"]) for inf in page.get_image_info() if pymupdf.Rect(inf["bbox"]).get_area() < 0.9 * W * H]
            except Exception:  # noqa: BLE001
                pass
            chrome, fig, body_lines = [], [], []
            for l in lines:
                sig = re.sub(r"\d+", "#", l.plain.lower())
                if (l.cy < 0.12 * H or l.cy > 0.88 * H) and sig in repeated:
                    chrome.append(l)
                elif any(r.contains(pymupdf.Point(l.cx, l.cy)) for r in imgs):
                    fig.append(l)                          # text drawn over a picture: it stays as it is
                else:
                    body_lines.append(l)
            regions: list[dict] = []
            if hints:
                _rects(parsed[i].get("items"), regions, hints=True)
            groups, rest = region_groups(body_lines, regions)          # Docling's regions decide where they have one ...
            for ch in candidate_chains(rest):                          # ... typography groups the rest
                groups.extend(split_chain(ch))
            for g in groups:
                f = _finish(make_frame(g, i, ""))
                f["_pieces"] = [[round(v, 1) for v in p] for l in g for p in getattr(l, "pieces", [l.bbox])]
                pf.append(f)
            infer_alignment_style(pf, W)
            _pop_sure(pf)
            body_frames = xy_cut(pf, key=lambda f: f["bbox"]) if pf else []
            for f in body_frames:
                f["role"] = _role(f, body)
            head, foot = [], []
            for l in sorted(chrome, key=lambda l: (l.bbox[1], l.bbox[0])):
                cf = _finish(make_frame([l], i, ""))
                cf["chrome"] = True
                cf["role"] = "header" if l.cy < H / 2 else "footer"
                (head if cf["role"] == "header" else foot).append(cf)
            pf = head + body_frames + foot
            untouched = [[round(v, 1) for v in l.bbox] for l in fig]
        # what stays printed: text over pictures and the frames that need no translation (numbers ...); the boxes of the
        # translated frames are pulled back from it, so erasing them does not take it along
        # A row joined from pieces is erased piece by piece, so the gap between them (where other text may sit) is not touched.
        keep_boxes = untouched + [p for f in pf if f.get("tgt") is not None for p in (f.get("_pieces") or f["lines"])]
        for k, f in enumerate(pf):
            f["id"] = f"p{i:04d}b{k:03d}"
            pieces = f.pop("_pieces", None) or f["lines"]
            f["cut"] = redaction_rects(pieces, [b for b in keep_boxes if b not in pieces])
        todo = sum(1 for f in pf if f.get("tgt") is None)
        out[i] = {"frames": pf, "untouched": untouched,
                  "report": {"items": todo, "frames": len(pf), "lost": [], "lost_n": 0, "coverage": None,
                             "lines": len(lines), "hints": hints}}
    return out

