"""PDF text layer -> lines with inline-format runs (bold / italic / link).

The text of the translated document comes from here, not from a parser: parsers that read page
images make OCR-like typos (a footer URL once came back as `tutorialsdoojo`), the text layer is exact. Inline formatting is turned into a small markup the translator must preserve:
    **bold**   *italic*   [text](https://url)
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

BULLETS = "●○•◦▪■□▶►‣·∙◆◇–—-*"
_BOLD = re.compile(r"bold|black|heavy|semibold|demi", re.I)
_ITALIC = re.compile(r"italic|oblique", re.I)
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯＀-￯]")
_NUM_MARKER = re.compile(r"^(\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|\(?[ivxIVX]{1,4}[.)])\s+")
_URL = re.compile(r"https?://\S+")


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    url: str | None = None


@dataclass
class Line:
    page: int
    bbox: list[float]          # x0, y0, x1, y1 (top-left origin, pt)
    size: float
    color: str
    runs: list[Run]
    bold_share: float
    marker: tuple[str, str] | None = None   # ("bullet"|"num", text)
    bbox_idx: int = 0
    tag: str | None = None
    base: float = 0.0          # baseline y of the dominant span (pt)

    @property
    def plain(self) -> str:
        return "".join(r.text for r in self.runs).strip()

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]


def _hex(c: int) -> str:
    return f"#{c & 0xFFFFFF:06x}"


def _span_attrs(s: dict) -> tuple[bool, bool]:
    f = s.get("flags", 0)
    name = s.get("font", "")
    return bool(f & 16) or bool(_BOLD.search(name)), bool(f & 2) or bool(_ITALIC.search(name))


def page_links(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, str]]:
    out = []
    try:
        for l in page.get_links():
            if l.get("kind") == pymupdf.LINK_URI and l.get("uri"):
                out.append((pymupdf.Rect(l["from"]), l["uri"]))
    except Exception:  # noqa: BLE001
        pass
    return out


def _segments(ln: dict) -> list[list[dict]]:
    """Split one PDF line wherever there is a wide horizontal gap (table cells, side-by-side text)."""
    segs: list[list[dict]] = []
    prev = None
    for s in ln["spans"]:
        if not s["text"].strip():
            if segs:
                segs[-1].append(s)
            continue
        r = pymupdf.Rect(s["bbox"])
        if prev is not None and r.x0 - prev.x1 > max(6.0, 1.5 * s["size"]):
            segs.append([])
        if not segs:
            segs.append([])
        segs[-1].append(s)
        prev = r
    return segs


def _marker_split(line: Line) -> None:
    """Detect a leading list marker, strip it from the runs and store it on the line."""
    if not line.runs:
        return
    first = line.runs[0].text.lstrip()
    if not first:
        return
    ch = first[0]
    if ch in "●○•◦▪■□▶►‣∙◆◇" or (ch in "–—-·*" and len(first) > 1 and first[1] == " "):
        rest = first[1:].lstrip()
        line.marker = ("bullet", ch)
        line.runs[0] = Run(rest, line.runs[0].bold, line.runs[0].italic, line.runs[0].url)
        if not rest:
            line.runs.pop(0)
        return
    m = _NUM_MARKER.match(first)
    if m:   # detected only; stripped later if (and only if) the line turns out to be a list item
        line.marker = ("num", m.group(1))


def page_lines(page: pymupdf.Page, page_idx: int) -> list[Line]:
    links = page_links(page)
    d = page.get_text("dict", flags=pymupdf.TEXT_MEDIABOX_CLIP)
    lines: list[Line] = []
    for blk in d["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines", []):
            dx, dy = ln.get("dir", (1, 0))
            if dx < 0.98 or abs(dy) > 0.2:   # rotated / vertical text is left alone
                continue
            for seg in _segments(ln):
                text = "".join(s["text"] for s in seg)
                if not text.strip():
                    continue
                rect = pymupdf.Rect(seg[0]["bbox"])
                for s in seg[1:]:
                    rect |= pymupdf.Rect(s["bbox"])
                cnt: Counter = Counter()
                runs: list[Run] = []
                bold_chars = 0
                total = 0
                for s in seg:
                    t = s["text"]
                    if not t:
                        continue
                    b, i = _span_attrs(s)
                    c = pymupdf.Rect(s["bbox"])
                    ctr = ((c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2)
                    url = next((u for r, u in links if r.x0 <= ctr[0] <= r.x1 and r.y0 <= ctr[1] <= r.y1), None)
                    n = len(t.strip())
                    cnt[(round(s["size"], 1), s["color"])] += n
                    total += n
                    bold_chars += n if b else 0
                    if runs and (runs[-1].bold, runs[-1].italic, runs[-1].url) == (b, i, url):
                        runs[-1].text += t
                    else:
                        runs.append(Run(t, b, i, url))
                if not total:
                    continue
                (size, color), _ = cnt.most_common(1)[0]
                line = Line(page_idx, [rect.x0, rect.y0, rect.x1, rect.y1], float(size), _hex(color), runs,
                            bold_chars / total)
                dom = max(seg, key=lambda s_: len(s_["text"].strip()))
                line.base = float(dom.get("origin", (0, rect.y1))[1])
                lines.append(line)
    # a lone bullet/number glyph on the same baseline as the next segment belongs to it
    lines.sort(key=lambda l: (round(l.cy), l.bbox[0]))
    merged: list[Line] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if (nxt and len(ln.plain) <= 3 and (ln.plain[:1] in BULLETS or _NUM_MARKER.match(ln.plain + " "))
                and abs(nxt.cy - ln.cy) < max(2.5, ln.height * 0.4) and 0 <= nxt.bbox[0] - ln.bbox[2] < 40):
            nxt.runs = [Run(ln.plain + " ", ln.runs[0].bold, False)] + nxt.runs
            nxt.bbox[0] = ln.bbox[0]
            i += 1
            continue
        merged.append(ln)
        i += 1
    for ln in merged:
        _marker_split(ln)
    return merged


# ----------------------------------------------------------------------------- markup
def _escape(t: str) -> str:
    return t.replace("*", "\\*")


def lines_to_markup(lines: list[Line], block_bold: bool) -> str:
    """Join lines of one paragraph into a single string with inline markup."""
    parts: list[Run] = []
    for i, ln in enumerate(lines):
        runs = [Run(r.text, r.bold, r.italic, r.url) for r in ln.runs if r.text != ""]
        if not runs:
            continue
        if parts:
            prev = parts[-1]
            nxt = runs[0]
            pt, nt = prev.text.rstrip(), nxt.text.lstrip()
            if pt.endswith("-") and nt[:1].islower() and not prev.url and len(pt) > 1 and pt[-2].isalpha():
                prev.text = pt[:-1]
                runs[0].text = nt
            elif prev.url and prev.url == nxt.url:         # a URL wrapped over two lines
                prev.text = pt
                runs[0].text = nt
            elif _CJK.search(pt[-1:]) and _CJK.search(nt[:1]):
                prev.text = pt
                runs[0].text = nt
            else:
                prev.text = pt + " "
                runs[0].text = nt
        parts.extend(runs)
    merged: list[Run] = []
    for r in parts:
        if merged and (merged[-1].bold, merged[-1].italic, merged[-1].url) == (r.bold, r.italic, r.url):
            merged[-1].text += r.text
        else:
            merged.append(Run(r.text, r.bold, r.italic, r.url))
    out = []
    for r in merged:
        core = r.text.strip()
        if not core:
            out.append(r.text)
            continue
        lead = r.text[: len(r.text) - len(r.text.lstrip())]
        trail = r.text[len(r.text.rstrip()):]
        body = _escape(core)
        if r.bold and not block_bold:
            body = f"**{body}**"
        if r.italic:
            body = f"*{body}*"
        if r.url:
            body = f"[{body}]({r.url})"
        out.append(lead + body + trail)
    text = "".join(out)
    return re.sub(r"[ \t]+", " ", text).strip()


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def strip_markup(s: str) -> str:
    s = _MD_LINK.sub(r"\1", s)
    s = re.sub(r"\\\*", "\u0000", s).replace("*", "").replace("\u0000", "*")
    return s


def md_to_markup(md: str) -> str:
    """Parser text (markdown-ish) -> our markup (used only for pages without a text layer)."""
    s = re.sub(r"</?[a-zA-Z][^>]*>", "", md)      # <u>, <font color=...> ...
    s = re.sub(r"^\s*(#{1,6}\s+|[*\-+]\s+|\d+[.)]\s+)", "", s)
    s = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~<>])", r"\1", s)
    s = s.replace("\n", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def strip_num_marker(line: Line) -> None:
    """Remove the leading "1." / "a)" from a line that is a list item (keeps it in `line.marker`)."""
    if line.marker and line.marker[0] == "num" and line.runs:
        first = line.runs[0].text.lstrip()
        m = _NUM_MARKER.match(first)
        if m:
            r = line.runs[0]
            line.runs[0] = Run(first[m.end():], r.bold, r.italic, r.url)
