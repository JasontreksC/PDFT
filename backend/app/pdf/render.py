"""Page → PNG rendering (for the viewer background)."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import pymupdf

from . import PYMU_LOCK

_docs: "OrderedDict[tuple, pymupdf.Document]" = OrderedDict()
_MAX_OPEN = 6


def _open(path: Path) -> pymupdf.Document:
    key = (str(path), path.stat().st_mtime_ns)
    doc = _docs.get(key)
    if doc is None:
        doc = pymupdf.open(path)
        _docs[key] = doc
        while len(_docs) > _MAX_OPEN:
            _, old = _docs.popitem(last=False)
            old.close()
    else:
        _docs.move_to_end(key)
    return doc


def render_png(pdf: Path, pno: int, scale: float) -> bytes:
    with PYMU_LOCK:
        page = _open(pdf)[pno]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")


def render_png_long(pdf: Path, pno: int, long_side: int) -> bytes:
    """Page pno as PNG whose longer edge is `long_side` px (the screenshot a vision model reads)."""
    with PYMU_LOCK:
        page = _open(pdf)[pno]
        r = page.rect
        z = max(0.3, min(6.0, long_side / max(r.width, r.height)))
        return page.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False).tobytes("png")


def forget(pdf: Path) -> None:
    with PYMU_LOCK:
        for key in [k for k in _docs if k[0] == str(pdf)]:
            _docs.pop(key).close()
