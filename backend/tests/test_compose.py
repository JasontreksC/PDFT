"""Layout modes of the composer. Needs a font: PDFT_TEST_FONT_DIR (or /app/fonts in the image), else skipped.
   cd backend && PDFT_TEST_FONT_DIR=/path/to/fonts python -m unittest tests.test_compose"""
import os
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.frames import build as fb
from app.frames.compose import compose
from app.pdf.fonts import FontManager

FONT_DIRS = [Path(p) for p in (os.environ.get("PDFT_TEST_FONT_DIR", ""), "/app/fonts") if p]
PARAS = [["The first paragraph of the page runs over three printed lines here,", "so that its translation, which is much",
          "shorter, frees two lines below it."],
         ["The second paragraph sits right under the first one and is long enough", "to be a real paragraph of two lines."],
         ["A third paragraph closes the page."]]


def fonts() -> FontManager | None:
    fm = FontManager([d for d in FONT_DIRS if d.is_dir()], Path(tempfile.mkdtemp()))
    return fm if fm.public() else None


class LayoutModeTest(unittest.TestCase):
    def setUp(self):
        self.fm = fonts()
        if self.fm is None:
            self.skipTest("no font (set PDFT_TEST_FONT_DIR)")
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "p.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=500, height=400)
        y = 60
        for para in PARAS:
            for ln in para:
                page.insert_text((60, y), ln, fontsize=11)
                y += 14
            y += 10
        doc.save(self.pdf)

    def tearDown(self):
        self.tmp.cleanup()

    def tops(self, flow: bool) -> list[tuple[float, float]]:
        pi = fb.read_page(self.pdf, 0)
        items = [{"id": i + 1, "layout": "paragraph", "lines": p, "align": "left",
                  "translation": "짧은 번역." if i == 0 else "번역문 " * (6 * len(p))} for i, p in enumerate(PARAS)]
        r = fb.frames_from_items(pi, items)
        self.assertEqual(len(r["frames"]), 3)
        doc = fb.build_document(self.pdf)
        doc["blocks"], doc["untouched"] = r["frames"], {"0": r["untouched"]}
        info = compose(doc, self.fm, None, Path(self.tmp.name) / "o.pdf", self.pdf, None, flow)
        return [(f["bbox"][1], info["positions"][f["id"]][0]["rect"][1]) for f in r["frames"]]

    def test_place_keeps_every_translation_where_its_source_was(self):
        for orig, drawn in self.tops(flow=False):
            self.assertAlmostEqual(orig, drawn, delta=1.5)

    def test_flow_pulls_the_text_below_a_shorter_translation_up(self):
        (_, _), (o2, d2), (o3, d3) = self.tops(flow=True)
        self.assertLess(d2, o2 - 10)
        self.assertLess(d3, o3 - 10)
