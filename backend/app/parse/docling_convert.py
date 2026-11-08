"""Docling -> the parser-result format the frame builder reads (an items tree per page, boxes in PDF points).

  {"items": {"pages": [{"page_number": 1, "success": True, "items": [ {type, md, bbox:[{x,y,w,h}]} ... ]}]},
   "credits": 0, "job_id": ..., "tier": "docling", "seconds": ...}

bbox are PDF points with a TOP-LEFT origin, like the page geometry the builder works in. This module needs the `docling`
package; it is imported by the local Docling service (docling_host/) or by the app itself when it runs natively.
"""
from __future__ import annotations

import time
from typing import Callable

_TYPE = {
    "title": "heading", "section_header": "heading",
    "page_header": "header", "page_footer": "footer",
    "picture": "image", "table": "table", "code": "code",
}


def make_converter(*, table_structure: bool = False, ocr: bool = True, device: str = "auto", threads: int = 8):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import AcceleratorDevice, AcceleratorOptions, PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_table_structure = table_structure
    opts.do_ocr = ocr
    opts.accelerator_options = AcceleratorOptions(num_threads=threads, device=getattr(AcceleratorDevice, device.upper()))
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def _label(item) -> str:
    lab = getattr(item, "label", None)
    return getattr(lab, "value", str(lab)) if lab is not None else "text"


def _table_cells(item, page_h: float, page_no: int) -> list[dict]:
    """Cells of a recognised table as text items (one box per cell) - only present when table structure is on."""
    out = []
    try:
        cells = item.data.table_cells
    except Exception:  # noqa: BLE001
        return out
    for c in cells or []:
        text = (getattr(c, "text", "") or "").strip()
        bb = getattr(c, "bbox", None)
        if not text or bb is None:
            continue
        try:
            bb = bb.to_top_left_origin(page_height=page_h)
        except Exception:  # noqa: BLE001
            pass
        w, h = bb.r - bb.l, bb.b - bb.t
        if w > 0 and h > 0:
            out.append({"type": "text", "md": text, "in_cell": True,
                        "bbox": [{"x": round(bb.l, 2), "y": round(bb.t, 2), "w": round(w, 2), "h": round(h, 2), "label": "table_cell"}]})
    return out


def _items_of(doc, page_heights: dict[int, float]) -> dict[int, list[dict]]:
    pages: dict[int, list[dict]] = {}
    for item, _level in doc.iterate_items():
        prov = getattr(item, "prov", None)
        if not prov:
            continue
        label = _label(item)
        p = prov[0]
        h = page_heights.get(p.page_no)
        if not h:
            continue
        bb = p.bbox.to_top_left_origin(page_height=h)
        box = {"x": round(bb.l, 2), "y": round(bb.t, 2), "w": round(bb.r - bb.l, 2), "h": round(bb.b - bb.t, 2), "label": label}
        if box["w"] <= 0 or box["h"] <= 0:
            continue
        text = (getattr(item, "text", "") or "").strip()
        typ = _TYPE.get(label, "text")
        if typ == "table":
            try:
                md = item.export_to_markdown(doc)
            except Exception:  # noqa: BLE001
                md = text
        else:
            md = text
        if typ == "heading" and md:
            md = "## " + md
        if typ != "image" and typ != "table" and not md:
            continue
        it: dict = {"type": typ, "md": md, "bbox": [box]}
        if typ == "table":
            cells = _table_cells(item, h, p.page_no)
            if cells:
                pages.setdefault(p.page_no, []).extend(cells)
        if label == "section_header" and getattr(item, "level", None):
            it["level"] = int(item.level)
        if label == "list_item":
            it = {"type": "list", "bbox": [box], "md": md, "items": [{"type": "text", "md": md, "bbox": [box]}]}
        pages.setdefault(p.page_no, []).append(it)
    return pages


def _flag_table_lines(pages: dict[int, list[dict]]) -> None:
    """With table-structure recognition off Docling reports the text of a table as one item per LINE next to the table
    item. Such lines are marked `in_table`: good as text (scans), useless as 'this is one box' hints."""
    for items in pages.values():
        tabs = [it["bbox"][0] for it in items if it["type"] == "table"]
        if not tabs:
            continue
        for it in items:
            if it["type"] in ("table", "image", "header", "footer"):
                continue
            b = it["bbox"][0]
            cx, cy = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
            if any(t["x"] - 2 <= cx <= t["x"] + t["w"] + 2 and t["y"] - 2 <= cy <= t["y"] + t["h"] + 2 for t in tabs):
                it["in_table"] = True


def convert_pdf(path, *, table_structure: bool = False, ocr: bool = True, device: str = "auto", threads: int = 8,
                chunk: int = 8, on_progress: Callable[[int, int], None] | None = None, converter=None) -> dict:
    """Convert a whole PDF (in page chunks, so progress can be reported and memory stays bounded)."""
    import pymupdf

    t0 = time.time()
    with pymupdf.open(path) as d:
        n = d.page_count
        sizes = {i + 1: d[i].rect.height for i in range(n)}
    conv = converter or make_converter(table_structure=table_structure, ocr=ocr, device=device, threads=threads)
    by_page: dict[int, list[dict]] = {}
    ok: dict[int, bool] = {}
    for a in range(1, n + 1, chunk):
        b = min(n, a + chunk - 1)
        try:
            res = conv.convert(str(path), page_range=(a, b), raises_on_error=False)
            doc = res.document
            chunk_items = _items_of(doc, sizes)
            _flag_table_lines(chunk_items)
            for pno, items in chunk_items.items():
                by_page[pno] = items
            for pno in range(a, b + 1):
                ok[pno] = True
        except Exception:  # noqa: BLE001 - one bad chunk must not lose the whole document
            for pno in range(a, b + 1):
                ok[pno] = False
        if on_progress:
            on_progress(b, n)
    pages = [{"page_number": i, "success": ok.get(i, False), "items": by_page.get(i, [])} for i in range(1, n + 1)]
    return {"items": {"pages": pages}, "credits": 0, "job_id": "docling", "tier": "docling",
            "seconds": round(time.time() - t0, 1)}
