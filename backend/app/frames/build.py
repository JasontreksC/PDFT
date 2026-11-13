"""Frames: the pieces of text the translated page is built from.

A *frame* is one translation unit that keeps its place on the ORIGINAL page: a wrapped paragraph, a heading, a caption, one
table cell ... In "고급 번역" (this module) frames come from the vision translation, not from typography heuristics:

  1. the page screenshot goes to the model, which answers with the text worth translating as {original, translation} items
  2. every `original` is matched against the page's own text layer (match.matcher); the text-layer lines it covers
     are the lines of the frame (their boxes, sizes, colours and bold/italic runs are what the composer needs)
  3. `tgt` of the frame is the model's translation

Text of the page that no item covers (formulas, numbers, labels the model left out) simply stays printed as it is.
How a longer or shorter translation is fitted into the page (font size, moving the text below, ...) is `frames.compose`.

In "기본 번역" the frames are built from the text layer by typography, with Docling's regions as hints (frames.basic),
and only their text goes to the model.

`build_document` makes the empty document skeleton (no LLM): which pages can be translated at all.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import pymupdf

from ..match.matcher import PageText, match_items, norm
from ..structure import textlayer as tl
from . import geometry

_URLISH = re.compile(r"^(https?://|www\.)\S+$|^\S+@\S+\.\S+$")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_SPLIT = re.compile(r"\s+[/|]\s+|\n+")                 # how a model joins the labels of a figure into one item

MATCH_THRESHOLD = 0.9


def needs_translation(text: str) -> bool:
    plain = tl.strip_markup(text).strip()
    letters = sum(c.isalpha() for c in plain)
    if letters == 0:
        return False
    if letters < 2 and not _CJK.search(plain):
        return False
    return not _URLISH.match(plain)


# ------------------------------------------------------------------------------ one frame
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


def _finish(f: dict) -> dict:
    f["kind"] = "text"
    f["style"] = {"font": None, "weight": f.pop("weight"), "size": f.pop("size"), "color": f.pop("color")}
    f["orig_size"] = f["style"]["size"]
    f["orig_style"] = dict(f["style"])
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
                f["align"], f["_sure"] = "left", True
            elif max(cxs) - min(cxs) <= tol:
                f["align"], f["_sure"] = "center", True
            elif max(rights) - min(rights) <= tol:
                f["align"], f["_sure"] = "right", True
            continue
        others = [o for o in frames if o is not f]
        nl = sum(1 for o in others if abs(o["bbox"][0] - x0) <= 1.5)
        nr = sum(1 for o in others if abs(o["bbox"][2] - x1) <= 1.5)
        nc = sum(1 for o in others if abs((o["bbox"][0] + o["bbox"][2]) / 2 - cx) <= 1.5)
        if abs(cx - W / 2) <= 2:
            nc += 2
        best = max((nl, 0, "left"), (nr, 1, "right"), (nc, 2, "center"))
        # A line that starts where other texts start (a list item, a paragraph) is left-aligned: it only happens to reach
        # the right margin when its text fills the line, and counting the justified paragraphs that end there must not
        # outvote that. Right / centre only for a line whose left edge is nobody else's.
        if best[0] >= 2 and best[2] != "left" and best[0] > nl and nl < 2:
            f["align"] = best[2]


def infer_alignment_style(frames: list[dict], W: float) -> None:
    """infer_alignment works on top-level fields; finished frames keep them in `style`."""
    for f in frames:
        f["size"] = f["style"]["size"]
    infer_alignment(frames, W)
    for f in frames:
        f.pop("size", None)


_ALIGN = {"left": "left", "justify": "left", "justified": "left", "center": "center", "centre": "center", "centered": "center",
          "right": "right"}


def apply_model_alignment(frames: list[dict], model_align: list[str | None]) -> None:
    """The model sees the page, so its answer wins over the guess from neighbouring frames - except for a paragraph (several
    lines) whose own line ends settle the question (that is measured, not guessed). An unknown or missing answer changes nothing."""
    for f, ma in zip(frames, model_align):
        a = _ALIGN.get(str(ma or "").strip().lower())
        if a and (f["n_lines"] == 1 or not f.get("_sure")):
            f["align"] = a
    for f in frames:
        f.pop("_sure", None)


def restore_links(src: str, tgt: str) -> str:
    """The model sees a picture, not the link annotations: a link whose text survives in the translation (a printed URL,
    a name left untranslated) gets its link back."""
    for m in _LINK.finditer(src):
        text, url = m.group(1), m.group(2)
        if f"[{text}]({url})" in tgt or text not in tgt:
            continue
        tgt = tgt.replace(text, f"[{text}]({url})", 1)
    return tgt


# ------------------------------------------------------------------------------ the skeleton
def verbatim_pages(doc: pymupdf.Document) -> set[int]:
    """Full-page-picture pages with almost no text (covers, plates) in a document that is mostly real text:
    they are copied unchanged."""
    n = doc.page_count
    words = [len(doc[i].get_text("words")) for i in range(n)]
    if sum(1 for w in words if w >= 20) < 0.5 * n:      # a scanned document: every page would have to be translated
        return set()
    keep = set()
    for i in range(n):
        if words[i] >= 30:
            continue
        if geometry.full_page_image(doc[i]):
            keep.add(i)
    return keep


def scan_pages_of(doc: pymupdf.Document, keep: set[int]) -> list[int]:
    out = []
    for i in range(doc.page_count):
        if i in keep or doc[i].rotation:
            continue
        if len(doc[i].get_text("words")) < 3:
            out.append(i)
    return out


def scan_pages(pdf) -> list[int]:
    """Pages that have no text layer at all (they cannot be matched, so they are left as they are)."""
    with pymupdf.open(pdf) as d:
        return scan_pages_of(d, verbatim_pages(d))


def _span(pages: list[int]) -> str:
    return ", ".join(str(i + 1) for i in pages[:8]) + ("…" if len(pages) > 8 else "")


def build_document(pdf) -> dict:
    """The document skeleton: no frames yet (they appear when a page is translated)."""
    with pymupdf.open(pdf) as doc:
        n = doc.page_count
        warnings: list[str] = []
        keep = verbatim_pages(doc)
        if keep:
            warnings.append(f"표지·전면 이미지 쪽 {len(keep)}개는 원본 그대로 유지합니다 (쪽 {_span(sorted(keep))})")
        rotated = [i for i in range(n) if doc[i].rotation and i not in keep]
        if rotated:
            warnings.append(f"회전된 쪽 {len(rotated)}개는 번역하지 않고 원본 그대로 둡니다 (쪽 {_span(rotated)})")
        scans = scan_pages_of(doc, keep)
        if scans:
            warnings.append(f"텍스트 층이 없는 스캔 쪽 {len(scans)}개는 번역하지 않고 원본 그대로 둡니다 (쪽 {_span(scans)})")
        skip = set(keep) | set(rotated) | set(scans)
        # pages whose text is only an invisible OCR layer over a page picture: the text cannot be removed from the
        # picture, so its place is painted over with the surrounding colour
        paint_pages = [i for i in range(n) if i not in skip and geometry.full_page_image(doc[i]) and geometry.hidden_text_share(doc[i]) > 0.5]
        sizes_pg = [(round(p.rect.width, 1), round(p.rect.height, 1)) for p in doc]
    page_size = Counter(sizes_pg).most_common(1)[0][0] if sizes_pg else (612.0, 792.0)
    return {"version": 2, "flow": "vision", "page_count": n, "page_size": list(page_size), "page_sizes": [list(s) for s in sizes_pg],
            "blocks": [], "pages": {}, "keep_pages": sorted(keep), "scan_pages": sorted(scans), "rotated_pages": rotated,
            "paint_pages": paint_pages, "untouched": {}, "stats": {"pages": n, "translatable": n - len(skip)}, "warnings": warnings}


def translatable_pages(doc: dict) -> set[int]:
    """Covers, scanned pages (no text layer: nothing is read by OCR) and rotated pages are left as they are, in both modes."""
    skip = set(doc.get("keep_pages") or []) | set(doc.get("scan_pages") or []) | set(doc.get("rotated_pages") or [])
    return {i for i in range(doc.get("page_count", 0)) if i not in skip}


# ------------------------------------------------------------------------------ one page
@dataclass
class PageInput:
    index: int
    W: float
    H: float
    lines: list[tl.Line]            # the text layer's lines (style, runs, markers)
    pt: PageText                    # the text layer's characters (matching + exact boxes)
    char_line: list[int]            # per char of pt.text: index of the Line it sits in (-1 = none)
    line_chars: list[int]           # per Line: how many chars of pt.text it holds
    _ids: dict = field(default_factory=dict, repr=False)

    def ids_of(self, li: int) -> list[int]:
        """The chars of pt.text that sit in Line `li`, in stream order."""
        if not self._ids:
            for i, l in enumerate(self.char_line):
                if l >= 0:
                    self._ids.setdefault(l, []).append(i)
        return self._ids.get(li, [])

    @property
    def text(self) -> str:
        """Plain text of the page (what a glossary lookup is run against)."""
        return " ".join(l.plain for l in self.lines)


def _assign_chars(pt: PageText, lines: list[tl.Line]) -> tuple[list[int], list[int]]:
    """Which Line does every character belong to (by the centre of its box)."""
    grid: dict[int, list[int]] = defaultdict(list)
    for li, l in enumerate(lines):
        for g in range(int(l.bbox[1] // 20), int(l.bbox[3] // 20) + 1):
            grid[g].append(li)
    out = [-1] * len(pt.text)
    count = [0] * len(lines)
    for i, b in enumerate(pt.boxes):
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        best, area = -1, 1e18
        for li in grid.get(int(cy // 20), ()):
            x0, y0, x1, y1 = lines[li].bbox
            if x0 - 0.6 <= cx <= x1 + 0.6 and y0 - 0.6 <= cy <= y1 + 0.6 and (x1 - x0) * (y1 - y0) < area:
                best, area = li, (x1 - x0) * (y1 - y0)
        out[i] = best
        if best >= 0:
            count[best] += 1
    return out, count


def read_page(pdf, index: int) -> PageInput:
    with pymupdf.open(pdf) as doc:
        page = doc[index]
        lines = tl.page_lines(page, index)
        pt = PageText.from_page(page)
        W, H = page.rect.width, page.rect.height
    cl, lc = _assign_chars(pt, lines)
    return PageInput(index, W, H, lines, pt, cl, lc)


_ROLE = {"heading": "heading", "title": "heading", "section_header": "heading", "list_item": "list_item", "list": "list_item",
         "bullet": "list_item", "header": "header", "footer": "footer", "page_header": "header", "page_footer": "footer",
         "page_number": "footer"}


def _role(layout: str | None) -> str:
    return _ROLE.get((layout or "").strip().lower().replace("-", "_").replace(" ", "_"), "text")


def _union(boxes) -> list[float]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _raw_text(pi: PageInput, ids: list[int], rect: list[float]) -> str:
    """The text printed inside `rect` between the first and the last of the chars `ids` (spaces included)."""
    pt = pi.pt
    lo, hi = pt.raw_of[min(ids)], pt.raw_of[max(ids)]
    x0, y0, x1, y1 = rect[0] - 0.5, rect[1] - 0.5, rect[2] + 0.5, rect[3] + 0.5
    out: list[str] = []
    prev = None
    for c, b in pt.raw[lo:hi + 1]:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            if prev is not None and b[0] - prev[2] > 0.25 * (b[3] - b[1]) and not c.isspace():
                out.append(" ")                       # a gap without a space char (separate segments: "2.1" ... title)
            out.append(c)
            prev = b
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _trim_line(pi: PageInput, li: int, rng: list[int]) -> tl.Line | None:
    """The text-layer line `li` cut down to the chars `rng` (its first matched char to its last), keeping its runs (bold, links).

    What the model left out at the start or the end of a line - a list number ("2."), a dot leader, a page number - is cut
    off the runs and stays printed. A single letter is not cut (a typo of the model at the edge is not a decision to leave it
    out). Returns None when the runs cannot be cut reliably (markers, ligatures ...): the caller then rebuilds the line from the
    characters.
    """
    ln = pi.lines[li]
    if ln.marker and ln.marker[0] != "num":
        return None
    ids = pi.ids_of(li)
    if not ids or rng[0] not in ids:
        return None
    pt = pi.pt
    r_lo, r_hi = pt.raw_of[ids[0]], pt.raw_of[ids[-1]]
    r0, r1 = pt.raw_of[rng[0]], pt.raw_of[rng[-1]]
    head = [c for c, _ in pt.raw[r_lo:r0] if not c.isspace()]
    tail = [c for c, _ in pt.raw[r1 + 1:r_hi + 1] if not c.isspace()]
    cut_head = len(head) if len(head) >= 2 or not any(c.isalpha() for c in head) else 0
    cut_tail = len(tail) if len(tail) >= 2 or not any(c.isalpha() for c in tail) else 0
    keep_lo, keep_hi = (rng[0] if cut_head else ids[0]), (rng[-1] if cut_tail else ids[-1])
    runs = [tl.Run(r.text, r.bold, r.italic, r.url) for r in ln.runs]
    n = cut_head                                               # drop that many visible chars from the start of the runs ...
    for r in runs:
        i = 0
        while i < len(r.text) and n > 0:
            n -= 0 if r.text[i].isspace() else 1
            i += 1
        r.text = r.text[i:]
    n = cut_tail                                               # ... and from the end
    for r in reversed(runs):
        i = len(r.text)
        while i > 0 and n > 0:
            n -= 0 if r.text[i - 1].isspace() else 1
            i -= 1
        r.text = r.text[:i]
    runs = [r for r in runs if r.text.strip()]
    if not runs:
        return None
    new = tl.Line(ln.page, list(ln.bbox), ln.size, ln.color, runs, ln.bold_share, ln.marker if not cut_head else None)
    new.base, new.bbox_idx, new.tag = ln.base, ln.bbox_idx, ln.tag
    if cut_head or cut_tail:
        box = _union([pt.boxes[i] for i in ids if keep_lo <= i <= keep_hi])
        new.bbox = [box[0], ln.bbox[1], box[2], ln.bbox[3]]
    if norm("".join(r.text for r in runs)) != "".join(pt.text[i] for i in ids if keep_lo <= i <= keep_hi):
        return None                                            # the runs do not say what the characters say: do not guess
    return new


def span_lines(pi: PageInput, span_ids: list[int], matched: set[int]) -> tuple[list[tl.Line], set[int]]:
    """The printed lines of one frame, cut to exactly the characters of the match.

    A text-layer line is only partly used when the model left out something on it: the dot leader and the page number
    of a table-of-contents entry, text beside it. That line is then shortened to the matched characters (its box and
    its text) and the rest stays printed as it is. Pieces that sit on the same row (a number and its title, which the
    PDF stores as separate segments) become ONE line. Returns (lines, ids of the chars the frame took).
    """
    per: dict[int, list[int]] = defaultdict(list)
    for i in span_ids:
        li = pi.char_line[i]
        if li >= 0:
            per[li].append(i)
    kept: list[tuple[int, list[int]]] = []
    for li, ids in per.items():
        hit = [i for i in ids if i in matched]
        if not hit or len(hit) / len(ids) < 0.4:          # nothing of it matched / another column's text inside the span
            continue
        kept.append((li, [i for i in ids if hit[0] <= i <= hit[-1]]))
    kept.sort(key=lambda k: (pi.lines[k[0]].cy, pi.lines[k[0]].bbox[0]))
    rows: list[list[tuple[int, list[int]]]] = []
    for k in kept:
        ln = pi.lines[k[0]]
        if rows and abs(pi.lines[rows[-1][0][0]].cy - ln.cy) <= 0.5 * min(ln.height, pi.lines[rows[-1][0][0]].height):
            rows[-1].append(k)
        else:
            rows.append([k])
    out: list[tl.Line] = []
    taken: set[int] = set()
    for row in rows:
        ids = [i for _, rng in row for i in rng]
        taken.update(ids)
        if len(row) == 1 and len(row[0][1]) >= pi.line_chars[row[0][0]]:
            out.append(pi.lines[row[0][0]])                  # the whole printed line: keep its runs (bold, links, bullet)
            continue
        if len(row) == 1 and len(row[0][1]) >= 0.5 * max(1, pi.line_chars[row[0][0]]):
            cut = _trim_line(pi, row[0][0], row[0][1])       # most of the line, minus what the model left out at its ends
            if cut is not None:
                taken.update(row[0][1])
                out.append(cut)
                continue
        rect = _union([pi.pt.boxes[i] for i in ids])
        text = _raw_text(pi, ids, rect)
        if not text:
            continue
        dom = pi.lines[max(row, key=lambda k: len(k[1]))[0]]
        ln = tl.Line(pi.index, rect, dom.size, dom.color, [tl.Run(text, dom.bold_share >= 0.6)], dom.bold_share)
        ln.base = dom.base
        out.append(ln)
    return out, taken


def leftovers(pi: PageInput, taken: set[int]) -> list[list[float]]:
    """Boxes of the printed text no frame took (formulas, numbers, dot leaders, page numbers, what the model left out):
    it stays as it is and counts as fixed when the translated text is laid out."""
    by_line: dict[int, list[int]] = defaultdict(list)
    for i, li in enumerate(pi.char_line):
        if li >= 0 and i not in taken:
            by_line[li].append(i)
    out: list[list[float]] = []
    for li, ids in by_line.items():
        if len(ids) >= pi.line_chars[li]:
            out.append([round(v, 1) for v in pi.lines[li].bbox])
            continue
        gap = 1.5 * pi.lines[li].size
        run = [ids[0]]
        for i in ids[1:]:
            if pi.pt.boxes[i][0] - pi.pt.boxes[run[-1]][2] > gap:
                out.append([round(v, 1) for v in _union([pi.pt.boxes[j] for j in run])])
                run = []
            run.append(i)
        out.append([round(v, 1) for v in _union([pi.pt.boxes[j] for j in run])])
    return out


def redaction_rects(lines: list[list[float]], keep: list[tuple]) -> list[list[float]]:
    """Where to erase the old text of a frame: its line boxes, pulled back from every character that must stay printed.

    Text redaction removes every glyph whose box touches the area. Line boxes are as tall as the font's ascent + descent,
    so in tightly set text a line's box reaches into the glyphs of the lines above and below - and erasing a paragraph took
    the list number printed in front of its first line ("3.") with it. Each box is therefore cut back: vertically from
    kept glyphs of other rows, horizontally from kept glyphs on its own row (a dot leader right behind a title). Every
    glyph of the frame's own line still crosses the narrower box, so it is still erased.
    """
    out: list[list[float]] = []
    for lb in lines:
        x0, y0, x1, y1 = lb
        cy, h = (y0 + y1) / 2, max(y1 - y0, 0.1)
        for k in keep:
            if k[2] <= x0 or k[0] >= x1 or k[3] <= y0 or k[1] >= y1:
                continue                                       # does not touch the box
            kc = (k[1] + k[3]) / 2
            if y0 <= kc <= y1:                                 # same row: pull the side back
                if (k[0] + k[2]) / 2 < (x0 + x1) / 2:
                    x0 = max(x0, k[2] + 0.05)
                else:
                    x1 = min(x1, k[0] - 0.05)
            elif kc < cy:                                      # a row above / below: pull the edge back
                y0 = max(y0, k[3] + 0.05)
            else:
                y1 = min(y1, k[1] - 0.05)
        if y1 - y0 < 0.2 * h:                                  # nothing sensible is left: a thin band through the middle
            y0, y1 = cy - 0.1 * h, cy + 0.1 * h
        if x1 > x0:
            out.append([round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)])
    return out


def _orig(it: dict) -> str:
    return str(it.get("original") or " ".join(str(x) for x in it.get("lines") or []))


def split_at_cuts(ids: list[int], cuts: list[int]) -> list[list[int]]:
    """`ids` (a match's chars, lines in reading order) cut into the pieces the matcher found to be apart on the page."""
    at = sorted({ids.index(c) for c in cuts if c in ids and ids.index(c) > 0})
    return [p for p in (ids[a:b] for a, b in zip([0] + at, at + [len(ids)])) if p]


def split_text(text: str, weights: list[int]) -> list[str]:
    """`text` shared out over pieces of the given weights, cutting at the blank nearest to each share (a translation cannot be
    split exactly where the source was; the flow from piece to piece is what matters)."""
    total = max(1, sum(weights))
    out, start, acc = [], 0, 0
    for w in weights[:-1]:
        acc += w
        goal = int(len(text) * acc / total)
        gaps = [m.start() for m in re.finditer(r"\s", text) if m.start() > start]
        cut = min(gaps, key=lambda g: abs(g - goal)) if gaps else len(text)
        out.append(text[start:cut].strip())
        start = cut
    out.append(text[start:].strip())
    return out


def frames_from_items(pi: PageInput, items: list[dict], *, paint: bool = False, threshold: float = MATCH_THRESHOLD) -> dict:
    """Items [{id, layout, original, translation}] of one page -> {"frames", "untouched", "report"}.

    Items whose original matches the text layer at >= `threshold` become frames, cut to exactly the matched characters; the
    others are reported and their text stays as printed.
    """
    res = match_items(pi.pt, items, threshold, detail=True)
    frames: list[dict] = []
    aligns: list[str | None] = []
    taken: set[int] = set()
    lost: list[dict] = []

    owner = res["owner"]

    def one(ids: list[int], matched: set[int], tgt: str, layout: str | None, align: str | None) -> bool:
        lines, used = span_lines(pi, ids, matched)
        if not lines:
            return False
        taken.update(used)
        f = _finish(make_frame(lines, pi.index, ""))
        f["tgt"] = restore_links(f["src"], tgt)
        role = _role(layout)
        f["role"] = role
        if role in ("header", "footer"):
            f["chrome"] = True
        if paint:
            f["paint"] = True
        frames.append(f)
        aligns.append(align)
        return True

    def add(span: dict | None, tgt: str, layout: str | None, align: str | None, who: int) -> bool:
        tgt = (tgt or "").strip()
        if not span or not tgt:
            return False
        matched = set(span["chars"]) - taken
        # the chars between the matched ones that another item took belong to that item
        ids = [i for i in span["span"] if owner[i] in (-1, who)]
        pieces = split_at_cuts(ids, span.get("cuts") or [])
        if len(pieces) < 2:
            return one(ids, matched, tgt, layout, align)
        # the text continues somewhere else (the next column): one frame per piece, the translation shared out by length
        shares = split_text(tgt, [sum(1 for i in p if i in matched) for p in pieces])
        return any([one(p, matched, t, layout, align) for p, t in zip(pieces, shares) if t.strip()])

    for who, (it, r) in enumerate(zip(items, res["items"])):
        if r.get("ignored"):                                  # nothing to translate in it (a lone page number ...): not a loss
            continue
        if not r["matched"]:
            lost.append({"original": _orig(it)[:160], "score": r["score"]})
            continue
        tgt = str(it.get("translation") or "")
        pieces = [p for p in _SPLIT.split(tgt) if p.strip()] if r.get("split") else []
        if r.get("split") and len(pieces) == len(r.get("parts") or []) > 1:
            ok = any([add(p.get("span"), t, it.get("layout"), it.get("align"), who) for p, t in zip(r["parts"], pieces)])
        else:
            ok = any([add(sp, tgt, it.get("layout"), it.get("align"), who) for sp in r.get("spans", [])[:1]])
        if not ok:
            lost.append({"original": _orig(it)[:160], "score": r["score"], "reason": "번역문 없음"})
    infer_alignment_style(frames, pi.W)                      # the guess from the frames' own edges / neighbours ...
    apply_model_alignment(frames, aligns)                    # ... and what the model saw on the page
    pt = pi.pt
    keep = [pt.boxes[i] for i in range(len(pt.text)) if i not in taken]     # what stays printed
    for k, f in enumerate(frames):
        f["id"] = f"p{pi.index:04d}f{k:03d}"
        f["cut"] = redaction_rects(f["lines"], keep)
    report = {"items": len(items) - sum(1 for r in res["items"] if r.get("ignored")), "frames": len(frames), "lost": lost[:40], "lost_n": len(lost),
              "coverage": res["coverage"], "lines": len(pi.lines)}
    return {"frames": frames, "untouched": leftovers(pi, taken), "report": report}
