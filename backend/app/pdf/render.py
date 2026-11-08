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


def forget(pdf: Path) -> None:
    with PYMU_LOCK:
        for key in [k for k in _docs if k[0] == str(pdf)]:
            _docs.pop(key).close()
