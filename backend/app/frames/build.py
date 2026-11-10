"""Frame builder.

A *frame* is one translation unit that keeps its place on the ORIGINAL page: the lines of one flowing text
(a wrapped paragraph, a heading, one table-of-contents title, one table cell, a page number ...).
Nothing is classified (paragraph / list / table ...). Two printed lines belong to the same frame only if
typography says the second one is the wrapped continuation of the first:

  * candidate link: the lower line sits right below, similar size, and shares an edge / the centre line
  * wrap test (the typographic rule itself): a line break inside a paragraph exists because the NEXT word
    did not fit - so the upper line must be filled up to the text block's right edge within one word
  * explicit markers (bullet glyphs, "1."), a bold<->regular change or a colon label start a new frame - unless the layout
    parser (Docling) puts both lines in ONE region (then it decides: same region = one frame, different regions = two)

Nothing is learned from the document (no measured leading): every decision looks at the two lines themselves, so a
short excerpt behaves like the whole book.
A wrong decision only moves a frame boundary; the text stays where it was printed.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter

import pymupdf

from ..structure import textlayer as tl
from ..structure.reading_order import xy_cut
from . import geometry

_URLISH = re.compile(r"^(https?://|www\.)\S+$|^\S+@\S+\.\S+$")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def needs_translation(text: str) -> bool:
    plain = tl.strip_markup(text).strip()
    letters = sum(c.isalpha() for c in plain)
    if letters == 0:
        return False
    if letters < 2 and not _CJK.search(plain):
        return False
    return not _URLISH.match(plain)


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


def split_chain(chain, region_of=None):
    """Cut a candidate chain into frames."""
    L, R = _bounds(chain)
    groups = [[chain[0][0]]]
    for (a, _), (b, al) in zip(chain, chain[1:]):
        cut = False
        reason = ""
        ra = region_of(a) if region_of is not None else None
        # one parser region = one text, except code: its lines are real line breaks and must stay separate
        same_region = ra is not None and ra == region_of(b) and region_of.regions[ra]["kind"] != "code"
        if b.marker:
            cut, reason = True, "marker"
        elif same_region:
            pass                                        # the layout parser says these two lines are ONE text: trust it over the soft rules below
        elif re.search(r"[:：]$", a.plain):
            cut, reason = True, "label"                 # "Cost:" - a label never wraps into the next line
        elif abs(a.bold_share - b.bold_share) > 0.6:
            cut, reason = True, "style"
        elif region_of is not None and region_of(a) is not None and region_of(b) is not None and region_of(a) != region_of(b):
            cut, reason = True, "region"
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
                cut, reason = True, "wrap"
        if cut:
            groups.append([b])
        else:
            groups[-1].append(b)
    return groups


def _dominant(lines):
    cnt: Counter = Counter()
    for l in lines:
        cnt[(round(l.size, 1), l.color)] += max(1, len(l.plain))
    (size, color), _ = cnt.most_common(1)[0]
    tot = sum(max(1, len(l.plain)) for l in lines)
    bold = sum(l.bold_share * max(1, len(l.plain)) for l in lines) / tot
    return size, color, bold


def make_frame(lines: list[tl.Line], page: int, fid: str) -> dict:
    size, color, bold = _dominant(lines)
    block_bold = bold >= 0.6
    first = lines[0]
    lead = ""
    if first.marker and first.marker[0] == "bullet":
        lead = first.marker[1] + " "
    src = lead + tl.lines_to_markup([tl.Line(l.page, list(l.bbox), l.size, l.color, [tl.Run(r.text, r.bold, r.italic, r.url) for r in l.runs], l.bold_share, l.marker) for l in lines], block_bold)
    bbox = [min(l.bbox[0] for l in lines), min(l.bbox[1] for l in lines), max(l.bbox[2] for l in lines), max(l.bbox[3] for l in lines)]
    pitch = statistics.median([b.cy - a.cy for a, b in zip(lines, lines[1:])]) if len(lines) > 1 else None
    indent = 0.0
    if len(lines) > 1:
        base = min(l.bbox[0] for l in lines[1:])
        indent = round(lines[0].bbox[0] - base, 1)
    f = {"id": fid, "page": page, "bbox": [round(v, 1) for v in bbox], "lines": [[round(v, 1) for v in l.bbox] for l in lines],
         "src": src, "tgt": None, "size": round(size, 1), "color": color, "weight": 700 if block_bold else 400,
         "pitch": round(pitch, 2) if pitch else None, "indent": indent, "align": "left", "n_lines": len(lines)}
    if not needs_translation(src):
        f["tgt"] = src
    return f


def infer_alignment(frames: list[dict], W: float) -> None:
    """Multi-line frames: from their own edges. One-line frames: from the edges they share with other frames."""
    for f in frames:
        x0, y0, x1, y1 = f["bbox"]
        cx = (x0 + x1) / 2
        if f["n_lines"] > 1:
            ls = f["lines"][1:] if f["indent"] else f["lines"]
            tol = 0.5 * f["size"]
            lefts = [l[0] for l in ls]
            rights = [l[2] for l in ls]
            cxs = [(l[0] + l[2]) / 2 for l in ls]
            if max(lefts) - min(lefts) <= tol:
                f["align"] = "left"
            elif max(cxs) - min(cxs) <= tol:
                f["align"] = "center"
            elif max(rights) - min(rights) <= tol:
                f["align"] = "right"
            continue
        others = [o for o in frames if o is not f]
        nl = sum(1 for o in others if abs(o["bbox"][0] - x0) <= 1.5)
        nr = sum(1 for o in others if abs(o["bbox"][2] - x1) <= 1.5)
        nc = sum(1 for o in others if abs((o["bbox"][0] + o["bbox"][2]) / 2 - cx) <= 1.5)
        if abs(cx - W / 2) <= 2:
            nc += 2
        best = max((nl, 0, "left"), (nr, 1, "right"), (nc, 2, "center"))
        if best[0] >= 2 and best[2] != "left" and best[0] > nl:
            f["align"] = best[2]


def doc_repeated(doc: pymupdf.Document, page_lines: dict[int, list[tl.Line]]) -> set[str]:
    n = doc.page_count
    if n < 3:
        return set()
    seen: Counter = Counter()
    for i, ls in page_lines.items():
        H = doc[i].rect.height
        seen.update({re.sub(r"\d+", "#", l.plain.lower()) for l in ls if l.cy < 0.12 * H or l.cy > 0.88 * H})
    return {t for t, c in seen.items() if c >= max(3, 0.5 * n) and t.strip()}



# ------------------------------------------------------------------------------ document
def verbatim_pages(doc: pymupdf.Document) -> set[int]:
    """Full-page-picture pages with almost no text (covers, plates) in a document that is mostly real text:
    they are copied unchanged."""
    n = doc.page_count
    words = [len(doc[i].get_text("words")) for i in range(n)]
    if sum(1 for w in words if w >= 20) < 0.5 * n:      # a scanned document: every page has to be translated
        return set()
    keep = set()
    for i in range(n):
        if words[i] >= 30:
            continue
        if geometry.full_page_image(doc[i]):
            keep.add(i)
    return keep


def scan_pages(pdf) -> list[int]:
    """Pages that have no text layer at all (to be read by the parser)."""
    with pymupdf.open(pdf) as d:
        return scan_pages_of(d, verbatim_pages(d))


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


def _region_of(regions: list[dict]):
    def f(line: tl.Line):
        best, area = None, 1e18
        for k, r in enumerate(regions):
            x0, y0, x1, y1 = r["rect"]
            if x0 - 2 <= line.cx <= x1 + 2 and y0 - 2 <= line.cy <= y1 + 2 and (x1 - x0) * (y1 - y0) < area:
                best, area = k, (x1 - x0) * (y1 - y0)
        return best
    f.regions = regions
    return f


def _scan_frame(r: dict, page: int, W: float) -> dict | None:
    md = r["md"]
    text = tl.md_to_markup(md)
    if not text.strip():
        return None
    x0, y0, x1, y1 = r["rect"]
    chars = max(len(tl.strip_markup(text)), 1)
    area = max((x1 - x0) * (y1 - y0), 1.0)
    size = max(6.0, min(28.0, (area / (chars * 0.62)) ** 0.5 / 1.12))
    if r["kind"] == "heading":
        size = min(28.0, size * 1.05)
    f = {"id": "", "kind": "text", "role": r["kind"], "page": page, "bbox": [round(v, 1) for v in (x0, y0, x1, y1)],
         "lines": [[round(v, 1) for v in (x0, y0, x1, y1)]], "src": text, "tgt": None,
         "style": {"font": None, "weight": 700 if r["kind"] == "heading" else 400, "size": round(size, 1), "color": "#000000"},
         "orig_size": round(size, 1), "pitch": None, "indent": 0.0, "align": "left", "n_lines": 1, "scan": True}
    f["orig_style"] = dict(f["style"])
    if not needs_translation(text):
        f["tgt"] = text
    return f


def _scan_line_frames(regs: list[dict], page: int, W: float = 612.0) -> list[dict]:
    """OCR'd single lines (md text + box) -> frames, via pseudo text-layer lines and the normal chaining rules."""
    if not regs:
        return []
    hs = sorted(r["rect"][3] - r["rect"][1] for r in regs)
    size = max(5.0, min(30.0, hs[len(hs) // 2] * 0.95))
    lines = []
    for r in regs:
        x0, y0, x1, y1 = r["rect"]
        text = tl.md_to_markup(r["md"])
        if not text.strip():
            continue
        ln = tl.Line(page, [x0, y0, x1, y1], size, "#000000", [tl.Run(text)], 0.0)
        ln.base = (y0 + y1) / 2 + 0.3 * size      # box centre, not bottom: descenders (g p y) move the bottom edge of OCR boxes
        tl._marker_split(ln)
        lines.append(ln)
    chains = candidate_chains(lines)
    out = []
    for ch in chains:
        for g in split_chain(ch):
            f = _finish(make_frame(g, page, ""))
            f["role"] = "text"
            f["scan"] = True
            out.append(f)
    infer_alignment_style(out, W)
    return out


def _finish(f: dict) -> dict:
    f["kind"] = "text"
    f["style"] = {"font": None, "weight": f.pop("weight"), "size": f.pop("size"), "color": f.pop("color")}
    f["orig_size"] = f["style"]["size"]
    f["orig_style"] = dict(f["style"])
    return f


def build_document(pdf, parsed: dict | None = None) -> dict:
    src = pymupdf.open(pdf)
    try:
        return _build(src, parsed)
    finally:
        src.close()


def _build(doc: pymupdf.Document, parsed: dict | None) -> dict:
    warnings: list[str] = []
    stats: Counter = Counter()
    n = doc.page_count
    parsed_pages = {p["page_number"] - 1: p for p in ((parsed or {}).get("items") or {}).get("pages", []) if p.get("success", True)}
    keep = verbatim_pages(doc)
    if keep:
        warnings.append(f"표지·전면 이미지 쪽 {len(keep)}개는 원본 그대로 유지합니다 (쪽 {', '.join(str(i + 1) for i in sorted(keep)[:8])}{'…' if len(keep) > 8 else ''})")
    scans = set(scan_pages_of(doc, keep))

    all_lines: dict[int, list[tl.Line]] = {}
    for i in range(n):
        if i in keep or i in scans or doc[i].rotation:
            all_lines[i] = []
            if doc[i].rotation and i not in keep:
                warnings.append(f"p{i + 1}: 회전된 페이지는 텍스트 레이어를 사용하지 않습니다")
        else:
            all_lines[i] = tl.page_lines(doc[i], i)
    sizes: Counter = Counter()
    for ls in all_lines.values():
        for l in ls:
            sizes[round(l.size, 1)] += len(l.plain)
    body = _weighted_median(sizes) or 10.0
    repeated = doc_repeated(doc, all_lines)

    # pass 1: candidate chains per page
    per_page: dict[int, dict] = {}
    for i in range(n):
        lines = all_lines[i]
        if not lines:
            continue
        page = doc[i]
        H, W = page.rect.height, page.rect.width
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
                fig.append(l)
            else:
                body_lines.append(l)
        chains = candidate_chains(body_lines)
        regions: list[dict] = []
        if i in parsed_pages:
            _rects(parsed_pages[i].get("items"), regions, hints=True)
        per_page[i] = {"chains": chains, "chrome": chrome, "fig": fig, "W": W, "H": H, "regions": regions}

    frames: list[dict] = []
    untouched: dict[str, list[list[float]]] = {}
    for i in range(n):
        page = doc[i]
        W, H = page.rect.width, page.rect.height
        pf: list[dict] = []
        if i in per_page:
            pg = per_page[i]
            region_of = _region_of(pg["regions"]) if pg["regions"] else None
            for ch in pg["chains"]:
                for g in split_chain(ch, region_of):
                    pf.append(make_frame(g, i, ""))
            pf = [_finish(f) for f in pf]
            infer_alignment_style(pf, W)
            body_frames = xy_cut(pf, key=lambda f: f["bbox"]) if pf else []
            chrome_frames = []
            for l in sorted(pg["chrome"], key=lambda l: (l.bbox[1], l.bbox[0])):
                cf = _finish(make_frame([l], i, ""))
                cf["chrome"] = True
                cf["role"] = "header" if l.cy < H / 2 else "footer"
                chrome_frames.append(cf)
            for f in body_frames:
                f["role"] = _role(f, body)
            head = [f for f in chrome_frames if f["role"] == "header"]
            foot = [f for f in chrome_frames if f["role"] == "footer"]
            pf = head + body_frames + foot
            untouched[str(i)] = [list(l.bbox) for l in pg["fig"]]
            stats["lines"] += len(all_lines[i])
            stats["chrome_lines"] += len(pg["chrome"])
        elif i in scans and i in parsed_pages:
            regs: list[dict] = []
            _rects(parsed_pages[i].get("items"), regs)
            for r in [r for r in regs if not r["line"]]:
                f = _scan_frame(r, i, W)
                if f:
                    pf.append(f)
            # lines of a table the parser did not structure (Docling): chain them into paragraphs with the same rules
            # that read the text layer, so a table cell is not translated one printed line at a time
            pf.extend(_scan_line_frames([r for r in regs if r["line"]], i, W))
            # the size of a scanned text is only estimated (area / characters): keep the body text consistent
            est = [f["style"]["size"] for f in pf if f["role"] != "heading"]
            if est:
                med = round(statistics.median(est), 1)
                for f in pf:
                    f["style"]["size"] = med if f["role"] != "heading" else round(max(f["style"]["size"], 1.2 * med), 1)
                    f["orig_size"] = f["style"]["size"]
                    f["orig_style"] = dict(f["style"])
            pf = xy_cut(pf, key=lambda f: f["bbox"]) if pf else []
            stats["scan_frames"] += len(pf)
        elif i in scans:
            stats["scan_pages_unparsed"] += 1
        frames.extend(pf)
    if scans and not parsed_pages:
        warnings.append(f"스캔 쪽 {len(scans)}개는 글자를 읽으려면 파서(Docling) 분석이 필요합니다 (지금은 원본 그대로)")
    elif scans:
        warnings.append(f"스캔 쪽 {len(scans)}개는 파서(OCR)가 읽은 글자로 번역합니다 (그림 속 글자는 번역하지 않음)")
    # pages whose text is only an invisible OCR layer over a page picture: the text cannot be removed from the
    # picture, so its place is painted over with the surrounding colour
    paint_pages = sorted(i for i in per_page if geometry.full_page_image(doc[i]) and geometry.hidden_text_share(doc[i]) > 0.5)
    for k, f in enumerate(frames):
        f["id"] = f"b{k:05d}"
        if f.get("scan") or f["page"] in paint_pages:
            f["paint"] = True
    sizes_pg = [(round(p.rect.width, 1), round(p.rect.height, 1)) for p in doc]
    page_size = Counter(sizes_pg).most_common(1)[0][0] if sizes_pg else (612.0, 792.0)
    stats["frames"] = len(frames)
    return {"version": 2, "page_count": n, "page_size": list(page_size), "page_sizes": [list(s) for s in sizes_pg],
            "body_size": round(body, 1), "blocks": frames, "keep_pages": sorted(keep), "scan_pages": sorted(scans),
            "paint_pages": paint_pages, "untouched": untouched, "stats": dict(stats), "warnings": warnings}


def scan_pages_of(doc: pymupdf.Document, keep: set[int]) -> list[int]:
    out = []
    for i in range(doc.page_count):
        if i in keep or doc[i].rotation:
            continue
        if len(doc[i].get_text("words")) < 3:
            out.append(i)
    return out


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


def infer_alignment_style(frames: list[dict], W: float) -> None:
    """infer_alignment works on top-level fields; finished frames keep them in `style`."""
    tmp = []
    for f in frames:
        f["size"] = f["style"]["size"]
        tmp.append(f)
    infer_alignment(tmp, W)
    for f in frames:
        f.pop("size", None)
