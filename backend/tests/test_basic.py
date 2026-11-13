"""기본 번역 (text layer + Docling layout hints + a text model), with a scripted model and a fake Docling (no network):
   cd backend && python -m unittest tests.test_basic"""
import re
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.config import ModelEntry, SettingsStore
from app.frames import basic
from app.frames import build as fb
from app.jobs import JobStore
from app.llm import clients as llm
from app.llm.clients import LLMResult
from app.parse import docling_client
from app.translate import pipeline, prompts

P1 = ("The quick brown fox jumps over the lazy dog while the encryption algorithm transforms every single block of the "
      "message into something nobody can read without the key.")
LABEL = "Cost:"
AFTER = "Twelve dollars per page, paid in advance."


def make_pdf(path: Path, pages: int = 2, scan: bool = False) -> None:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=400, height=500)
        page.insert_textbox((20, 20, 380, 50), "Sample Heading", fontsize=16, fontname="hebo")
        page.insert_textbox((20, 60, 300, 140), P1, fontsize=10)
        page.insert_text((20, 170), LABEL, fontsize=10)
        page.insert_text((20, 182), AFTER, fontsize=10)
        page.insert_text((20, 220), "1234", fontsize=10)
    if scan:
        doc.new_page(width=400, height=500)          # no text layer at all
    doc.save(path)


def _region(x0, y0, x1, y1, typ="text", md="x"):
    return {"type": typ, "md": md, "bbox": [{"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "label": typ}]}


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "b.pdf"
        make_pdf(self.pdf)

    def tearDown(self):
        self.tmp.cleanup()

    def frames(self, parsed=None):
        return basic.build_pages(self.pdf, parsed, {0})[0]

    def test_wrapped_paragraph_is_one_frame_and_numbers_stay(self):
        r = self.frames()
        srcs = [f["src"] for f in r["frames"]]
        para = next(f for f in r["frames"] if f["src"].startswith("The quick"))
        self.assertGreater(para["n_lines"], 1)                       # the wrapped lines are one text
        self.assertIn("1234", srcs)
        self.assertEqual(next(f for f in r["frames"] if f["src"] == "1234")["tgt"], "1234")   # nothing to translate: kept
        self.assertTrue(all(f["id"].startswith("p0000b") for f in r["frames"]))
        self.assertTrue(all("cut" in f for f in r["frames"]))
        self.assertEqual(r["report"]["hints"], False)

    def test_without_hints_a_label_line_starts_its_own_frame(self):
        srcs = [f["src"] for f in self.frames()["frames"]]
        self.assertIn(LABEL, srcs)
        self.assertIn(AFTER, srcs)

    def test_a_docling_region_joins_what_the_rules_would_cut(self):
        parsed = {0: {"success": True, "items": [_region(15, 158, 300, 186)]}}      # "Cost:" + the next line = ONE region
        r = self.frames(parsed)
        self.assertIn(f"{LABEL} {AFTER}", [f["src"] for f in r["frames"]])
        self.assertTrue(r["report"]["hints"])
        self.assertEqual(len(basic.parser_boxes(parsed[0])), 1)

    def test_a_scan_is_not_translated(self):
        pdf = Path(self.tmp.name) / "s.pdf"
        make_pdf(pdf, pages=1, scan=True)
        doc = fb.build_document(pdf)
        self.assertEqual(doc["scan_pages"], [1])
        self.assertEqual(fb.translatable_pages(doc), {0})
        parsed = {1: {"success": True, "items": [_region(20, 80, 300, 120, "text", "Some scanned body text here.")]}}
        self.assertNotIn(1, basic.build_pages(pdf, parsed, {1}))        # even with regions: no text layer, no frames

class RegionTest(unittest.TestCase):
    """Docling's region decides, whatever the typography: everything whose centre is in one region is one frame."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "r.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=500, height=400)
        page.insert_text((20, 40), "2.1", fontsize=10)                       # one row, two pieces far apart
        page.insert_text((120, 40), "Glossary of terms", fontsize=10)
        page.insert_text((20, 80), "Big lead-in", fontsize=16)                # different sizes, one region
        page.insert_text((20, 100), "small continuation text", fontsize=9)
        page.insert_text((20, 140), "- first point", fontsize=10)             # bullets in one region
        page.insert_text((20, 154), "- second point", fontsize=10)
        page.insert_text((20, 200), "Cell one:", fontsize=10)                  # inside a table area: no hint
        page.insert_text((20, 214), "cell two words", fontsize=10)
        doc.save(self.pdf)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, items):
        return {f["src"]: f for f in basic.build_pages(self.pdf, {0: {"success": True, "items": items}}, {0})[0]["frames"]}

    def test_pieces_of_a_row_in_one_region_are_one_frame(self):
        no = self.build([])
        self.assertIn("2.1", no)                                             # the rules alone: apart
        fr = self.build([_region(15, 28, 300, 46)])
        f = fr["2.1 Glossary of terms"]
        self.assertEqual(f["n_lines"], 1)
        self.assertEqual(len(f["cut"]), 2)                                   # erased piece by piece, not the gap
        self.assertLess(f["cut"][0][2], 60)

    def test_different_sizes_in_one_region_are_one_frame(self):
        fr = self.build([_region(15, 64, 300, 104)])
        self.assertIn("Big lead-in small continuation text", fr)

    def test_bullets_in_one_region_keep_their_marks(self):
        fr = self.build([_region(15, 128, 300, 158)])
        self.assertIn("- first point - second point", fr)

    def test_a_table_area_is_no_hint(self):
        fr = self.build([_region(15, 188, 300, 218, "table")])
        self.assertIn("Cell one:", fr)
        self.assertIn("cell two words", fr)


def t_reply(user: str, skip: set[int] = frozenset()) -> LLMResult:
    ids = [int(m) for m in re.findall(r'<t id="(\d+)"', user)]
    body = "\n".join(f'<t id="{i}">번역{i}</t>' for i in ids if i not in skip)
    return LLMResult(body + '\n<term src="fox">여우</term>', 100, 50, False)


class PipelineBasicTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = JobStore(root / "jobs")
        self.job = self.store.create("t.pdf")
        self.job.dir.mkdir(parents=True, exist_ok=True)
        make_pdf(self.job.source, pages=2, scan=True)
        self.job.meta["pages"] = [{"w": 400, "h": 500}] * 3
        self.job.set_doc(fb.build_document(self.job.source))
        self.runner = pipeline.Runner(SettingsStore(root / "settings.json"))
        self.entry = ModelEntry(id="m", name="m", type="openai", endpoint="http://x", api_key="k", model="m", context_window=100000)
        self.users: list[str] = []
        self.sent: list[list[int]] = []
        self._orig = (llm.chat, docling_client.health, docling_client.parse_pdf_docling)

    def tearDown(self):
        llm.chat, docling_client.health, docling_client.parse_pdf_docling = self._orig
        self.tmp.cleanup()

    def model(self, skip_first: set[int] = frozenset()):
        first = [True]

        async def fake(entry, system, user, **kw):
            self.assertIn('<t id="N" type="...">', system)          # the text prompt, not the vision one
            self.users.append(user)
            skip = skip_first if first[0] else set()
            first[0] = False
            return t_reply(user, skip)
        llm.chat = fake

    def docling(self, up: bool):
        async def health(timeout=3.0):
            return {"ok": True, "device": "cpu", "docling": "2"} if up else None

        async def parse(path, *, table_structure=False, on_status=None):
            with pymupdf.open(path) as d:
                n = d.page_count
                self.sent.append(n)
            pages = [{"page_number": k + 1, "success": True, "items": [_region(15, 158, 300, 186)]} for k in range(n)]
            if on_status:
                on_status("1/1쪽")
            return {"items": {"pages": pages}}
        docling_client.health = health
        docling_client.parse_pdf_docling = parse

    async def run_all(self, conc=1):
        self.runner.start(self.job, self.entry, "Korean", False, conc, "basic")
        await self.runner.tasks[self.job.id]
        return self.job.meta

    async def test_pages_are_translated_with_docling_hints(self):
        self.model()
        self.docling(True)
        meta = await self.run_all()
        self.assertEqual(meta["status"], "done", meta.get("error"))
        doc = self.job.doc()
        self.assertEqual(self.sent, [2])                                   # the two text pages once; the scan is not sent
        self.assertEqual(set(pipeline.load_parse(self.job)), {0, 1})      # cached per page index
        texts = {b["src"]: b["tgt"] for b in doc["blocks"] if b["page"] == 0}
        self.assertIn(f"{LABEL} {AFTER}", texts)                           # joined by the region
        self.assertTrue(texts[f"{LABEL} {AFTER}"].startswith("번역"))
        self.assertEqual(texts["1234"], "1234")                            # never sent
        self.assertTrue(all("1234" not in u for u in self.users))
        self.assertEqual(doc["pages"]["0"]["mode"], "basic")
        self.assertTrue(doc["pages"]["0"]["hints"])
        self.assertFalse([b for b in doc["blocks"] if b["page"] == 2])     # the scan stays as it is
        self.assertEqual(pipeline.page_states(doc), [2, 2, 3])
        # the heading repeats on page 2: translated once, reused from the memo
        self.assertEqual(sum(u.count("Sample Heading") for u in self.users), 1)
        self.assertIn("여우", [g["tgt"] for g in self.job.context().glossary.values()])   # terms of the text answers

    async def test_cached_regions_are_not_sent_again(self):
        self.model()
        self.docling(True)
        self.job.set_selection([0])
        await self.run_all()
        self.job.set_selection(None)
        self.job.meta["status"] = "paused"
        await self.run_all()
        self.assertEqual(self.sent, [1, 1])                                 # page 0 alone, then only page 1

    async def test_without_docling_the_rules_alone_group_the_lines(self):
        self.model()
        self.docling(False)
        meta = await self.run_all()
        self.assertEqual(meta["status"], "done", meta.get("error"))
        self.assertIn("Docling", meta["progress"].get("notice", ""))
        doc = self.job.doc()
        self.assertEqual(pipeline.page_states(doc), [2, 2, 3])
        self.assertFalse(doc["pages"]["0"]["hints"])
        self.assertIn(LABEL, [b["src"] for b in doc["blocks"] if b["page"] == 0])     # the rules alone: label apart

    async def test_a_missing_translation_is_asked_again(self):
        self.model(skip_first={1})
        self.docling(True)
        self.job.set_selection([0])
        await self.run_all()
        self.assertEqual(len(self.users), 2)
        self.assertNotIn('<t id="0"', self.users[1])                         # only the gap is asked again
        self.assertIn('<t id="1"', self.users[1])
        self.assertFalse(self.job.doc()["pages"]["0"]["lost"])

    async def test_parallel_pages(self):
        self.model()
        self.docling(True)
        meta = await self.run_all(conc=4)
        self.assertEqual(meta["status"], "done", meta.get("error"))


class PromptTest(unittest.TestCase):
    def test_text_prompt_round_trip(self):
        u = prompts.text_user("", "", 1, 2, [(0, "text", "a </t> b"), (1, "heading", "H")])
        self.assertIn('<t id="0" type="text">a < /t> b</t>', u)
        self.assertEqual(prompts.parse_translations('<t id="0">가</t>\n<t id="1"> 나 </t>'), {0: "가", 1: "나"})
        self.assertNotEqual(pipeline.system_prompt("Korean", "basic"), pipeline.system_prompt("Korean", "advanced"))


if __name__ == "__main__":
    unittest.main()
