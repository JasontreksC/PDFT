"""Match the sentences a vision LLM read off a page screenshot against the PDF's own text layer.

For every LLM item (its printed text, line by line) we look for the characters of the text layer that say (almost) the same
thing and take the bounding box of exactly those characters. Nothing but the standard library is needed.

Pipeline
  1. `PageText.from_page`: the page's characters in content-stream order, each with its box, normalised so that whitespace,
     case, ligatures, quote/dash variants do not matter (the LLM and the PDF never agree on those).
  2. `align_item`: a sequence alignment of the item's characters against the characters of the page that are still free.
     k-gram anchors propose, for every character of the item, the places of the page it may be; a dynamic programme then
     chooses one place per character so that the whole is the best path. Every step from one character to the next is scored
     by where the two characters sit on the page (see `_step`): right next to each other on the same row, the start of the
     row below where the model reported a line break, ... are rewarded; a jump to a neighbouring table cell, up the page,
     into another column is punished. The model's line breaks (`lines`) are a soft hint, never a hard rule.
     Text and place are therefore decided together, and the order in which the PDF happens to store its characters does not
     matter (tables are stored row by row, columns in any order).
  3. matched characters -> union bbox + one box per PDF line.  score >= threshold means "this is a text block".
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field

import pymupdf

_INVISIBLE = re.compile(r"[\s­​-‏⁠﻿]+")
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
                         "‘": "'", "’": "'", "“": '"', "”": '"'})
# markup the LLM adds that the PDF does not contain (markdown emphasis, LaTeX-ish sub/superscripts)
_LLM_NOISE = re.compile(r"[_{}^\\$`*]")


def norm(s: str) -> str:
    """Comparison form: NFKC, case-folded, no whitespace / invisible chars, ASCII-ish quotes and dashes."""
    s = unicodedata.normalize("NFKC", s).translate(_DASHES).casefold()
    return _INVISIBLE.sub("", s)


def norm_llm(s: str) -> str:
    return norm(_LLM_NOISE.sub("", s))


@dataclass
class Geo:
    """Per character of the page text: the numbers the alignment needs."""
    cx: list[float]
    cy: list[float]
    h: list[float]
    x0: list[float]
    x1: list[float]
    seg_lo: list[int]       # first char of the piece of the line this char sits in (pieces are cut at wide gaps)
    seg_hi: list[int]       # one past the last char of that piece


@dataclass
class PageText:
    text: str                                   # normalised stream
    boxes: list[tuple[float, float, float, float]]   # per char of `text`
    line_of: list[int]                          # per char of `text`: index into `lines`
    lines: list[dict] = field(default_factory=list)  # {"bbox": [x0,y0,x1,y1], "text": str, "start": int, "end": int}
    raw: list = field(default_factory=list, repr=False)     # from_page: every char of the stream, spaces too: (char, box)
    raw_of: list = field(default_factory=list, repr=False)  # from_page: index into `raw` of each char of `text`
    _index: dict = field(default_factory=dict, repr=False)
    _geo: Geo | None = field(default=None, repr=False)

    @classmethod
    def from_page(cls, page: pymupdf.Page) -> "PageText":
        d = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)    # no image blocks
        out: list[str] = []
        boxes: list[tuple] = []
        line_of: list[int] = []
        lines: list[dict] = []
        raw: list[tuple] = []
        raw_of: list[int] = []
        for blk in d["blocks"]:
            if blk.get("type") != 0:
                continue
            for ln in blk.get("lines", []):
                start = len(out)
                raw_txt: list[str] = []
                rect = None
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        c = ch["c"]
                        raw_txt.append(c)
                        bb = tuple(ch["bbox"])
                        raw.append((c, bb))
                        n = norm(c)
                        if not n:
                            continue
                        r = pymupdf.Rect(bb)
                        rect = r if rect is None else rect | r
                        for x in n:
                            out.append(x)
                            boxes.append(bb)
                            line_of.append(len(lines))
                            raw_of.append(len(raw) - 1)
                if len(out) > start and rect is not None:
                    lines.append({"bbox": [rect.x0, rect.y0, rect.x1, rect.y1], "text": "".join(raw_txt).strip(),
                                  "start": start, "end": len(out)})
                # a line that produced no visible char gets no line id; nothing points at it
        return cls("".join(out), boxes, line_of, lines, raw, raw_of)

    @classmethod
    def from_lines(cls, lines) -> "PageText":
        """From text-layer `Line` objects (structure.textlayer): a char's box is its line's box. Line index = index in `lines`."""
        out: list[str] = []
        boxes: list[tuple] = []
        line_of: list[int] = []
        info: list[dict] = []
        for li, ln in enumerate(lines):
            start = len(out)
            bb = tuple(ln.bbox)
            for ch in ln.plain:
                for x in norm(ch):
                    out.append(x)
                    boxes.append(bb)
                    line_of.append(li)
            info.append({"bbox": list(ln.bbox), "text": ln.plain, "start": start, "end": len(out)})
        return cls("".join(out), boxes, line_of, info)

    def kgram_index(self, k: int) -> dict[str, list[int]]:
        idx = self._index.get(k)
        if idx is None:
            idx = defaultdict(list)
            t = self.text
            for i in range(len(t) - k + 1):
                idx[t[i:i + k]].append(i)
            self._index[k] = idx
        return idx

    def geo(self) -> Geo:
        if self._geo is None:
            b = self.boxes
            lo = list(range(len(b)))
            hi = [i + 1 for i in range(len(b))]
            for ln in self.lines:
                run = ln["start"]
                for i in range(ln["start"] + 1, ln["end"] + 1):
                    if i < ln["end"]:
                        a, c = b[i - 1], b[i]
                        # a wide gap or a jump back ends a piece (table cells printed on one baseline, a number and its title)
                        if not (c[0] - a[2] > max(6.0, 1.5 * (c[3] - c[1])) or c[0] < a[0] - 2 * (a[3] - a[1])):
                            continue
                    for j in range(run, i):
                        lo[j], hi[j] = run, i
                    run = i
            self._geo = Geo([(x[0] + x[2]) / 2 for x in b], [(x[1] + x[3]) / 2 for x in b], [max(x[3] - x[1], 1.0) for x in b],
                            [x[0] for x in b], [x[2] for x in b], lo, hi)
        return self._geo


@dataclass
class Match:
    score: float                       # 0..1: text score minus the price of the jumps in the path
    start: int                         # span in PageText.text
    end: int
    chars: list[int]                   # matched char positions in PageText.text (sorted)
    exact: bool = False
    span: list[int] | None = None      # the chars of every PDF line the path touched, between its first and last matched one, in
                                       # reading (path) order: the unmatched ones between belong to the same text
    text_score: float = 0.0            # 2*matched/(len(item)+matched+extra), before the jumps are charged
    jumps: float = 0.0                 # weighted count of steps that cannot be "the next piece of the same text"
    cuts: list[int] = field(default_factory=list)   # chars (positions in pt.text) where the path jumped: a new piece starts there
    text: str = ""                     # the normalised item text that was looked for
    breaks: list[int] | None = None    # positions in `text` where the model's next line starts (None = no lines given)
    extra: int = 0                     # free chars of the same PDF lines that sit between matched ones (they lower the score)
    path: list = field(default_factory=list)   # [(k, p, kind, value, expected, quality)]: item char k sits at page char p; how the
                                       # step from the previous one was judged (kind: start / cont / wrap / far)


# ------------------------------------------------------------------------------ the step between two characters
# value of a step from one matched character to the next (the +1 for a matched character is added on top):
_CONT = 0.25            # next char on the same row, right behind (no line break expected / no information)
_CONT_BROKEN = -0.4     # ... but the model reported a line break here
_WRAP_EXPECTED = 0.5    # start of the row below, where the model reported a line break
_WRAP_UNKNOWN = 0.15    # ... when the model gave no lines
_WRAP_UNEXPECTED = -0.1  # ... the model saw no break here (its lines may be wrong: a small price only)
_FAR = -2.0             # anywhere else: neighbouring cell, up the page, another column
_FAR_BROKEN = -0.8      # ... at a place where the model reported a line break (a column may continue elsewhere)
_MAX_SKIP_CANDS = 4     # how many candidate-bearing item positions back a step may reach (the others are skipped chars)


def _step(g: Geo, a: int, b: int, skipped: int) -> tuple[int, float]:
    """How char b may follow char a. -> (kind, quality): kind 0 = continues the row, 1 = starts the row below, 2 = far.

    `skipped` item chars sit between them (typos, glyphs the PDF spells differently): the allowed distance grows with it.
    All distances are in units of the text height, so the same rules hold for body text and for footnotes.
    """
    h = max(g.h[a], g.h[b])
    dy = g.cy[b] - g.cy[a]
    slack = 1.2 * h * skipped
    gap = g.x0[b] - g.x1[a]
    if abs(dy) <= 0.7 * h:
        right = g.cx[b] > g.cx[a] + 1e-3 or (abs(g.cx[b] - g.cx[a]) <= 1e-3 and b > a)    # equal boxes: line-level geometry
        if right and -0.8 * h <= gap <= 4.0 * h + slack:
            q = 1.0 if gap <= h else max(0.3, 1.0 - 0.7 * (gap - h) / (3.0 * h + slack))
            return 0, q
    elif dy > 0 and dy <= 4.5 * h and g.x0[b] <= g.x1[a] + 0.5 * h + slack:
        q = 1.0 if dy <= 2.2 * h else max(0.3, 1.0 - 0.7 * (dy - 2.2 * h) / (2.3 * h))
        # a real wrap leaves the end of a piece and arrives at the start of one
        ends = 1.0 if g.seg_hi[a] - a <= 2 else 0.4
        starts = 1.0 if b - g.seg_lo[b] <= 1 else 0.4
        return 1, q * (ends + starts) / 2
    return 2, 0.0


def _value(kind: int, q: float, expected: bool, known: bool) -> tuple[float, float]:
    """(price/reward, jump weight) of a step. `known`: the model reported its line breaks; `expected`: one lies here."""
    if kind == 0:
        return (_CONT_BROKEN, 0.0) if known and expected else (_CONT * q, 0.0)
    if kind == 1:
        if not known:
            return _WRAP_UNKNOWN * q, 0.0
        return (_WRAP_EXPECTED * q, 0.0) if expected else (_WRAP_UNEXPECTED, 0.0)
    return (_FAR_BROKEN, 0.2) if known and expected else (_FAR, 1.0)


def _states(pt: PageText, alive: bytearray, t: str, anchor: int | None) -> dict[int, list[int]]:
    """For every position k of the item text that has a place on the page: the places (positions in pt.text) it may be at.

    A place is proposed by every k-gram of the item that reads the same there (all of its chars still free); the places with
    the most supporting grams are kept. Short items use plain occurrences.
    """
    L = len(t)
    K = min(3 if L < 14 else 4, L)
    idx = pt.kgram_index(K)
    big, cap = (200, 64) if L <= 12 else (40, 20)
    cand: list[dict[int, int]] = [defaultdict(int) for _ in range(L)]
    for i in range(L - K + 1):
        hits = idx.get(t[i:i + K])
        if not hits or len(hits) > big:
            continue
        for q in hits:
            if alive[q:q + K].count(1) != K:
                continue
            for j in range(K):
                cand[i + j][q + j] += 1
    # chars no gram vouches for (a dot leader, a run of dashes - grams made of them are everywhere - or a typo): when the char
    # before has a place, the char right behind that place may be this one
    t_text = pt.text
    for k in range(1, L):
        if cand[k] or not cand[k - 1]:
            continue
        for p in cand[k - 1]:
            if p + 1 < len(t_text) and alive[p + 1] and t_text[p + 1] == t[k]:
                cand[k][p + 1] += 1
    out: dict[int, list[int]] = {}
    for k in range(L):
        c = cand[k]
        if not c:
            continue
        ps = list(c.items())
        if len(ps) > cap:
            ps.sort(key=lambda kv: (-kv[1], abs(kv[0] - anchor) if anchor is not None else 0))
            ps = ps[:cap]
        out[k] = [p for p, _ in ps]
    return out


JUMP_PRICE = 0.15       # score lost per unit of jump weight: one jump nobody announced sinks even a perfect text (1.0 -> 0.85)


def align_item(pt: PageText, alive: bytearray, t: str, breaks: list[int] | None, anchor: int | None = None) -> Match | None:
    """Best place of the item text `t` among the free characters `alive`. `breaks`: positions in `t` where the model's next
    line starts (None = it gave no lines). Positions are those of pt.text."""
    L = len(t)
    if L < 2 or not pt.text:
        return None
    states = _states(pt, alive, t, anchor)
    if not states:
        return None
    g = pt.geo()
    known = breaks is not None
    brk = sorted(breaks or [])
    N = max(1, len(pt.text))
    ks = sorted(states)
    best: dict[tuple[int, int], tuple[float, tuple[int, int] | None, float]] = {}
    for jx, k in enumerate(ks):
        for p in states[k]:
            emit = 1.0 - (0.02 * min(1.0, abs(p - anchor) / N) if anchor is not None else 0.0)
            bv, bprev, bj = emit, None, 0.0
            for jy in range(max(0, jx - _MAX_SKIP_CANDS), jx):
                k2 = ks[jy]
                expected = known and bisect_right(brk, k) - bisect_right(brk, k2) > 0     # a break in (k2, k]
                for p2 in states[k2]:
                    if p2 == p:
                        continue
                    v2, _, j2 = best[(k2, p2)]
                    kind, q = _step(g, p2, p, k - k2 - 1)
                    tv, jw = _value(kind, q, expected, known)
                    val = v2 + emit + tv
                    if val > bv:
                        bv, bprev, bj = val, (k2, p2), j2 + jw
            best[(k, p)] = (bv, bprev, bj)
    end = max(best, key=lambda s: best[s][0])
    path: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = end
    while cur is not None:
        path.append(cur)
        cur = best[cur][1]
    path.reverse()
    jumps = best[end][2]
    chars = sorted({p for _, p in path})
    M = len(chars)
    matched = set(chars)
    extra = 0
    for (_, p0), (_, p1) in zip(path, path[1:]):         # free chars of the same PDF line that sit between two matched ones
        if p1 - p0 > 1 and pt.line_of[p0] == pt.line_of[p1] and p1 - p0 <= 80:
            extra += sum(1 for i in range(p0 + 1, p1) if alive[i] and i not in matched)
    text_score = min(1.0, 2.0 * M / (L + M + extra))
    score = max(0.0, text_score - JUMP_PRICE * jumps)
    spans: dict[int, list[int]] = {}
    for _, p in path:                                    # per PDF line, in the order the path visits them
        sp = spans.setdefault(pt.line_of[p], [p, p])
        sp[0], sp[1] = min(sp[0], p), max(sp[1], p)
    span = [i for lo, hi in spans.values() for i in range(lo, hi + 1) if alive[i]]
    cuts = [p1 for (k0, p0), (k1, p1) in zip(path, path[1:]) if _step(g, p0, p1, k1 - k0 - 1)[0] == 2]
    steps: list[tuple] = []
    for n, (k, p) in enumerate(path):
        if n == 0:
            steps.append((k, p, "start", 0.0, False, 1.0))
            continue
        k0, p0 = path[n - 1]
        kind, q = _step(g, p0, p, k - k0 - 1)
        expected = known and bisect_right(brk, k) - bisect_right(brk, k0) > 0
        steps.append((k, p, ("cont", "wrap", "far")[kind], _value(kind, q, expected, known)[0], expected, q))
    return Match(score, chars[0], chars[-1] + 1, chars, exact=text_score >= 0.9999 and jumps == 0, span=span,
                 text_score=text_score, jumps=jumps, cuts=cuts, text=t, breaks=breaks, extra=extra, path=steps)


# ------------------------------------------------------------------------------ geometry of a match
def _union(boxes) -> list[float]:
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes); y1 = max(b[3] for b in boxes)
    return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]


def lines_hit(pt: PageText, mt: Match) -> list[int]:
    """Indices (into pt.lines) of the PDF lines this match covers, in reading order (the order the path visited them).

    A line counts only if enough of its characters inside the span were matched, so text of another column that happens
    to sit inside the span does not stretch the box.
    """
    matched = set(mt.chars)
    per_line: dict[int, list[int]] = {}
    for i in (mt.span if mt.span is not None else range(mt.start, mt.end)):
        per_line.setdefault(pt.line_of[i], []).append(i)
    hit = []
    for li, ids in per_line.items():
        if sum(1 for i in ids if i in matched) / len(ids) >= 0.4:
            hit.append(li)
    if not hit and mt.chars:
        hit = [pt.line_of[mt.chars[0]]]
    return hit


def geometry(pt: PageText, mt: Match) -> tuple[list[float], list[list[float]], str, list[int]]:
    """(union bbox, per-line boxes, text of the PDF lines hit, line indices) for a match."""
    ids = lines_hit(pt, mt)
    span = set(mt.span if mt.span is not None else range(mt.start, mt.end))
    boxes: list[list[float]] = []
    for li in ids:
        chars = [i for i in range(pt.lines[li]["start"], pt.lines[li]["end"]) if i in span]
        boxes.append(_union([pt.boxes[i] for i in chars]) if chars else list(pt.lines[li]["bbox"]))
    return _union(boxes), boxes, "\n".join(pt.lines[li]["text"] for li in ids), ids


def rows_of(boxes: list[list[float]]) -> int:
    """How many printed rows a set of line boxes makes (boxes beside each other on one baseline are one row)."""
    rows: list[list[float]] = []
    for b in sorted(boxes, key=lambda b: (b[1] + b[3]) / 2):
        cy, h = (b[1] + b[3]) / 2, max(b[3] - b[1], 1.0)
        if rows and abs(cy - rows[-1][0]) <= 0.5 * min(h, rows[-1][1]):
            continue
        rows.append([cy, h])
    return len(rows)


# ------------------------------------------------------------------------------ claims
class Claims:
    """Which characters of the page are still free. A placed item takes its characters; nobody else can match them again."""

    def __init__(self, pt: PageText):
        self.pt = pt
        self.alive = bytearray(b"\x01") * len(pt.text)
        self.owner = [-1] * len(pt.text)

    def fork(self) -> "Claims":
        c = Claims.__new__(Claims)
        c.pt, c.alive, c.owner = self.pt, bytearray(self.alive), self.owner
        return c

    def take(self, ids: list[int], who: int = -2) -> None:
        for i in ids:
            self.alive[i] = 0
            if who >= 0:
                self.owner[i] = who


_SPLIT = re.compile(r"\s+[/|]\s+|\n+")     # how an LLM joins labels of a figure / table into one "original"
# a dot leader and the page number after it, at the end of a line (table of contents): never part of the text to translate,
# whatever the model copied
_LEADER = re.compile(r"\s*(?:[.·•∙‧…]\s*){4,}\s*(?:\d{1,5}|[ivxlc]{1,6})?\s*$", re.I)


def strip_leader(s: str) -> str:
    return _LEADER.sub("", s)


def _prepare(it: dict) -> tuple[str, list[int] | None]:
    """(text to look for, positions where the model's next printed line starts - None when it gave no lines)."""
    lines = it.get("lines")
    if isinstance(lines, list):
        segs = [s for s in (norm_llm(strip_leader(str(x))) for x in lines) if s]
        if segs:
            br, n = [], 0
            for s in segs[:-1]:
                n += len(s)
                br.append(n)
            return "".join(segs), br
    return norm_llm(strip_leader(str(it.get("original") or ""))), None


def _try(pt: PageText, claims: Claims, original: str, t: str, breaks: list[int] | None, threshold: float, anchor: int | None):
    """-> (score, parts, split, aligned): the item as a whole, or - for labels joined with " / " - piece by piece."""
    whole = align_item(pt, claims.alive, t, breaks, anchor)
    parts = [whole] if whole else []
    score = whole.score if whole else 0.0
    aligned: list[Match | None] = []                        # split items: one slot per piece (None = piece not found)
    split = False
    if _SPLIT.search(original):
        pieces = [x for x in (norm_llm(y) for y in _SPLIT.split(original)) if x]
        if len(pieces) > 1:
            fk = claims.fork()
            got: list[Match | None] = []
            for x in pieces:
                mt = align_item(pt, fk.alive, x, None, anchor)
                got.append(mt)
                if mt and mt.score >= threshold:
                    fk.take(mt.chars)
            weight = sum(len(x) for x in pieces)
            wscore = sum((m.score if m else 0.0) * len(x) for m, x in zip(got, pieces)) / max(1, weight)
            pmin = min((m.score if m else 0.0) for m in got)
            # the model joined separate labels with " / ": when every piece is found on its own, they ARE separate texts
            # (even if the whole string also matches, because the pieces happen to follow each other in the stream)
            if wscore > score or (wscore >= threshold and pmin >= threshold):
                parts, score, split, aligned = [m for m in got if m], wscore, True, got
    return score, parts, split, aligned


def trace_of(pt: PageText, m: Match) -> dict:
    """Everything the alignment decided about one match, for a viewer: per matched char where it sits and how the step to it
    was judged; the free chars between matched ones; the score's parts."""
    chars = set(m.chars)
    r1 = lambda b: [round(v, 1) for v in b]
    return {"norm": m.text, "breaks": m.breaks, "known": m.breaks is not None,
            "path": [{"k": k, "p": p, "c": pt.text[p], "box": r1(pt.boxes[p]), "kind": kind, "v": round(v, 2), "exp": exp, "q": round(q, 2)}
                     for k, p, kind, v, exp, q in m.path],
            "extra": [r1(pt.boxes[i]) for i in (m.span or []) if i not in chars][:300],
            "matched_n": len(chars), "extra_n": m.extra, "text_score": round(m.text_score, 4), "jumps": round(m.jumps, 2),
            "score": round(m.score, 4)}


def match_items(pt: PageText, items: list[dict], threshold: float = 0.9, detail: bool = False, trace: bool = False) -> dict:
    """items: [{"id", "layout", "original", "translation", "lines"?}]. Returns {"items": [...], "uncovered": [...], "coverage": float}.

    `lines` (optional): the printed text line by line, as the model saw it; without it `original` is used and no line break
    is known. Items are placed longest first (a long text is the most distinctive); the characters of a placed item are
    removed from the free ones before the next one is looked for, and an item that does not fit yet is retried after the
    others took theirs (up to three rounds).

    An item that looks like several pieces joined with " / " or newlines (labels of a figure) is also matched piece by piece:
    if every piece is found on its own (or the pieces score better than the whole), the item is split into one frame per piece.
    detail=True adds, per item, "spans": [{"start", "end", "chars", "span"}] (char positions in pt.text, one span per
    frame-to-be) and, for split items, a "span" inside every entry of "parts"; plus the top-level "owner" (per char: index of
    the item that took it, -1 = nobody). trace=True adds, per item, "traces": one `trace_of` per piece (also for items that failed:
    it shows the best attempt) - char-by-char what the alignment did.
    """
    n = len(items)
    prepared = [_prepare(it) for it in items]
    texts = [p[0] for p in prepared]
    ignored = [not any(c.isalpha() for c in t) for t in texts]       # a lone number, symbols, a dot leader: nothing to translate
    claims = Claims(pt)
    best: list[tuple | None] = [None] * n
    done = [False] * n
    order = sorted(range(n), key=lambda i: -len(texts[i]))      # stable: equal lengths keep the model's order

    def anchor_of(i: int) -> int | None:
        for j in range(i - 1, -1, -1):
            if done[j]:
                return max(m.end for m in best[j][1])
        for j in range(i + 1, n):
            if done[j]:
                return min(m.start for m in best[j][1])
        return None

    for _round in range(3):
        progress = False
        for i in order:
            if done[i] or ignored[i]:
                continue
            original = str(items[i].get("original") or "") or " ".join(str(x) for x in items[i].get("lines") or [])
            info = _try(pt, claims, original, texts[i], prepared[i][1], threshold, anchor_of(i))
            if info[1] and info[0] >= threshold:
                for m in info[1]:
                    claims.take(m.chars, i)
                best[i], done[i], progress = info, True, True
            elif best[i] is None or info[0] > best[i][0]:
                best[i] = info
        if not progress:
            break

    results: list[dict] = []
    for i, it in enumerate(items):
        r = {k: it.get(k) for k in ("id", "layout", "original", "translation")}
        r.update(ignored=ignored[i], score=0.0, matched=False, bbox=None, line_boxes=[], pdf_text="", chars=0, split=False, line_ids=[], jumps=0.0,
                 text_score=0.0, rows=0, model_lines=len(it["lines"]) if isinstance(it.get("lines"), list) else None)
        score, parts, split, aligned = best[i] if best[i] else (0.0, [], False, [])
        if parts:
            geos = [geometry(pt, m) for m in parts]
            lines = [lb for g in geos for lb in g[1]]
            ids = []
            for g in geos:
                ids.extend(li for li in g[3] if li not in ids)
            r.update(score=round(score, 4), bbox=_union(lines), line_boxes=lines, pdf_text="\n".join(g[2] for g in geos),
                     chars=sum(len(m.chars) for m in parts), matched=done[i], split=split, line_ids=ids,
                     jumps=round(sum(m.jumps for m in parts), 2), text_score=round(min(m.text_score for m in parts), 4),
                     rows=rows_of(lines))
            if trace:
                r["traces"] = [trace_of(pt, m) for m in parts]
            if detail:
                r["spans"] = [{"start": m.start, "end": m.end, "chars": m.chars, "span": m.span, "cuts": m.cuts} for m in parts]
            if split:
                r["parts"] = [{"line_ids": geometry(pt, m)[3] if m is not None and m.score >= threshold else [],
                               "score": round(m.score, 4) if m is not None else 0.0,
                               **({"span": {"start": m.start, "end": m.end, "chars": m.chars, "span": m.span, "cuts": m.cuts}}
                                  if detail and m is not None and m.score >= threshold else {})} for m in aligned]
        results.append(r)

    used = [o >= 0 for o in claims.owner]
    uncovered = []
    uncovered_ids = []
    covered = 0
    total = 0
    for li, ln in enumerate(pt.lines):
        cnt = ln["end"] - ln["start"]
        u = sum(used[ln["start"]:ln["end"]])
        total += cnt
        covered += u
        if u / max(1, cnt) < 0.5:
            uncovered.append({"bbox": [round(v, 2) for v in ln["bbox"]], "text": ln["text"]})
            uncovered_ids.append(li)
    out = {"items": results, "uncovered": uncovered, "uncovered_ids": uncovered_ids, "coverage": round(covered / max(1, total), 4)}
    if detail:
        out["owner"] = claims.owner
    return out
