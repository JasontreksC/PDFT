"""Page geometry shared by the builder and the composer: what is drawn on the page besides text."""
from __future__ import annotations

import pymupdf


def obstacles(page: pymupdf.Page, W: float, H: float) -> tuple[list[pymupdf.Rect], list[pymupdf.Rect]]:
    """(printed things that block growth: rules, boxes, pictures,  containers a text may live inside: boxes, pictures)"""
    blockers: list[pymupdf.Rect] = []
    containers: list[pymupdf.Rect] = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        drawings = []
    for d in drawings:
        r = pymupdf.Rect(d["rect"])
        if r.width * r.height > 0.8 * W * H:
            continue
        if r.height < 0.6:
            r = pymupdf.Rect(r.x0, r.y0 - 0.3, r.x1, r.y0 + 0.3)
        if r.width < 0.6:
            r = pymupdf.Rect(r.x0 - 0.3, r.y0, r.x0 + 0.3, r.y1)
        blockers.append(r)
        if r.width > 12 and r.height > 8:
            containers.append(r)
    try:
        for inf in page.get_image_info():
            r = pymupdf.Rect(inf["bbox"])
            if r.width * r.height <= 0.9 * W * H:
                blockers.append(r)
                containers.append(r)
    except Exception:  # noqa: BLE001
        pass
    return blockers, containers


def full_page_image(page: pymupdf.Page) -> bool:
    W, H = page.rect.width, page.rect.height
    try:
        return any(pymupdf.Rect(i["bbox"]).width >= 0.9 * W and pymupdf.Rect(i["bbox"]).height >= 0.9 * H
                   for i in page.get_image_info())
    except Exception:  # noqa: BLE001
        return False


def hidden_text_share(page: pymupdf.Page) -> float:
    """Share of the page's characters that are invisible (render mode 3: the OCR layer of a scanned page)."""
    try:
        tr = page.get_texttrace()
    except Exception:  # noqa: BLE001
        return 0.0
    tot = sum(len(t.get("chars", ())) for t in tr)
    hid = sum(len(t.get("chars", ())) for t in tr if t.get("type") == 3)
    return hid / tot if tot else 0.0


def background_color(page: pymupdf.Page, rect: pymupdf.Rect) -> tuple[float, float, float]:
    """Median colour of the ring just outside `rect` (what the page looks like around a piece of text)."""
    r = pymupdf.Rect(rect) + (-3, -3, 3, 3)
    r &= page.rect
    if r.is_empty:
        return (1.0, 1.0, 1.0)
    pix = page.get_pixmap(clip=r, matrix=pymupdf.Matrix(1, 1), alpha=False)
    w, h, n, s = pix.width, pix.height, pix.n, pix.samples
    px = []
    for y in range(h):
        for x in range(w):
            inner = 3 <= x < w - 3 and 3 <= y < h - 3
            if not inner:
                i = (y * w + x) * n
                px.append((s[i], s[i + 1], s[i + 2]) if n >= 3 else (s[i],) * 3)
    if not px:
        return (1.0, 1.0, 1.0)
    px.sort(key=lambda p: sum(p))
    m = px[len(px) // 2]
    return (m[0] / 255, m[1] / 255, m[2] / 255)
