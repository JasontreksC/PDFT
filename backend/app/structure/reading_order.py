"""Reading order by recursive XY-cut on region boxes.

Why not trust a parser's order: parsers have put a table between the two text columns or the body before
the title. XY-cut is deterministic and handles the usual cases (title band -> columns -> full-width blocks).

Rule: at each step find the widest whitespace gap along x (a column gutter) and along y (a gap
between bands). Cut at the vertical gutter if it is at least 0.8x as wide as the best horizontal gap,
otherwise at the horizontal gap. Regions left/top come first.
"""
from __future__ import annotations

MIN_GAP_X = 6.0   # pt: narrower than this is not a column gutter
MIN_GAP_Y = 1.0
VERTICAL_BIAS = 0.8


def _best_gap(intervals: list[tuple[float, float]], min_gap: float) -> tuple[float, float] | None:
    s = sorted(intervals)
    best = None
    cur_hi = s[0][1]
    for lo, hi in s[1:]:
        gap = lo - cur_hi
        if gap >= min_gap and (best is None or gap > best[0]):
            best = (gap, (cur_hi + lo) / 2)
        cur_hi = max(cur_hi, hi)
    return best


def _fallback(items: list[dict], key) -> list[dict]:
    return sorted(items, key=lambda r: (round(key(r)[1]), key(r)[0]))


def xy_cut(items: list[dict], key=lambda r: r["bbox"]) -> list[dict]:
    """Order `items`; `key(item)` -> [x0, y0, x1, y1]."""
    if len(items) <= 1:
        return list(items)
    gx = _best_gap([(key(r)[0], key(r)[2]) for r in items], MIN_GAP_X)
    gy = _best_gap([(key(r)[1], key(r)[3]) for r in items], MIN_GAP_Y)
    if gx and (not gy or gx[0] >= VERTICAL_BIAS * gy[0]):
        cut = gx[1]
        first = [r for r in items if key(r)[2] <= cut]
        second = [r for r in items if key(r)[2] > cut]
    elif gy:
        cut = gy[1]
        first = [r for r in items if key(r)[3] <= cut]
        second = [r for r in items if key(r)[3] > cut]
    else:
        return _fallback(items, key)
    if not first or not second:
        return _fallback(items, key)
    return xy_cut(first, key) + xy_cut(second, key)
