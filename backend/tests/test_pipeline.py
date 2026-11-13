"""Page translation paths with a scripted model (no network):  cd backend && python -m unittest tests.test_pipeline"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.config import ModelEntry, SettingsStore
from app.frames import build as fb
from app.jobs import JobStore
from app.llm import vision
from app.llm.clients import LLMResult
from app.translate import pipeline

P1 = "The quick brown fox jumps over the lazy dog while the encryption algorithm transforms every single block of the message."
P2 = "A second paragraph that talks about completely different matters such as hyphenation and typography rules."
HEAD = "3.4 The Sample Heading"


def make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page(width=400, height=500)
        page.insert_textbox((20, 20, 380, 50), HEAD, fontsize=14, fontname="hebo")
        page.insert_textbox((20, 60, 380, 130), P1, fontsize=10)
        page.insert_textbox((20, 140, 380, 210), P2, fontsize=10)
        page.insert_textbox((20, 260, 380, 280), "s_{r,c} = in[r + 4c]  (3.3)", fontsize=10)
    doc.save(path)


def answer(items, terms=None, truncated=False):
    return LLMResult(json.dumps({"items": items, "terms": terms or []}, ensure_ascii=False), 100, 50, truncated)


def item(i, layout, original, translation="번역문입니다"):
    return {"id": i, "layout": layout, "original": original, "translation": translation}


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = JobStore(root / "jobs")
        self.job = self.store.create("t.pdf")
        self.job.dir.mkdir(parents=True, exist_ok=True)
        make_pdf(self.job.source)
        self.job.meta["pages"] = [{"w": 400, "h": 500}] * 2
        self.job.set_doc(fb.build_document(self.job.source))
        self.settings = SettingsStore(root / "settings.json")
        self.runner = pipeline.Runner(self.settings)
        self.entry = ModelEntry(id="m", name="m", type="openai", endpoint="http://x", api_key="k", model="m", context_window=100000)
        self.calls = []
        self._orig = vision.chat_with_image

    def tearDown(self):
        vision.chat_with_image = self._orig
        self.tmp.cleanup()

    def script(self, replies):
        replies = list(replies)

        async def fake(entry, system, user, png, **kw):
            self.calls.append(user)
            self.assertTrue(png.startswith(b"\x89PNG"))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        vision.chat_with_image = fake

    async def run_all(self, pages_expected=2):
        self.runner.start(self.job, self.entry, "Korean", False, 1)
        await self.runner.tasks[self.job.id]
        return self.job.meta

    async def test_happy_path_makes_frames_and_leaves_equation(self):
        ok = [item(1, "heading", HEAD, "3.4 샘플 제목"), item(2, "paragraph", P1), item(3, "paragraph", P2)]
        self.script([answer(ok, [{"src": "algorithm", "tgt": "알고리즘"}]), answer(ok)])
        meta = await self.run_all()
        self.assertEqual(meta["status"], "done")
        doc = self.job.doc()
        self.assertEqual(len([b for b in doc["blocks"] if b["page"] == 0]), 3)
        heads = [b for b in doc["blocks"] if b["page"] == 0 and b["role"] == "heading"]
        self.assertEqual(heads[0]["tgt"], "3.4 샘플 제목")
        self.assertTrue(any(l[1] > 250 for l in doc["untouched"]["0"]))      # the equation line stays as printed
        self.assertEqual(pipeline.page_states(doc), [2, 2])
        self.assertIn("algorithm", self.job.context().glossary)

    async def test_answer_with_printed_lines_instead_of_original(self):
        words = P1.split()
        half = len(words) // 2
        lines_item = {"id": 2, "layout": "paragraph", "lines": [" ".join(words[:half]), " ".join(words[half:])], "translation": "본문입니다"}
        ok = [{"id": 1, "layout": "heading", "lines": [HEAD], "translation": "3.4 샘플 제목"}, lines_item,
              {"id": 3, "layout": "paragraph", "lines": [P2], "translation": "둘째 문단"}]
        self.script([answer(ok), answer(ok)])
        meta = await self.run_all()
        self.assertEqual(meta["status"], "done")
        doc = self.job.doc()
        self.assertEqual(len([b for b in doc["blocks"] if b["page"] == 0]), 3)
        self.assertEqual(pipeline.page_states(doc), [2, 2])

    async def test_cut_off_answer_is_continued(self):
        first = answer([item(1, "heading", HEAD), item(2, "paragraph", P1)], truncated=True)
        rest = answer([item(3, "paragraph", P2)])
        self.script([first, rest, answer([item(1, "heading", HEAD)])])
        await self.run_all()
        self.assertEqual(len([b for b in self.job.doc()["blocks"] if b["page"] == 0]), 3)
        self.assertIn("Continue with the item", self.calls[1])
        self.assertIn(P1[-30:], self.calls[1])

    async def test_unreadable_answer_is_retried_then_given_up(self):
        bad = LLMResult("I cannot help with that", 10, 5, False)
        self.script([bad, bad, bad, bad,          # page 1: all four tries unreadable
                     answer([item(1, "heading", HEAD)])])
        meta = await self.run_all()
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(pipeline.page_states(self.job.doc()), [0, 2])
        self.assertEqual(meta["status"], "paused")
        self.assertEqual(meta["progress"]["warnings"], 1)

    async def test_empty_answer_is_a_translated_page_without_frames(self):
        self.script([answer([]), answer([])])
        meta = await self.run_all()
        self.assertEqual(meta["status"], "done")
        self.assertEqual(pipeline.page_states(self.job.doc()), [2, 2])
        self.assertEqual(self.job.doc()["blocks"], [])

    async def test_hallucinated_original_is_reported_not_placed(self):
        self.script([answer([item(1, "paragraph", P1), item(2, "paragraph", "This sentence is not on the page at all, honestly.")]),
                     answer([])])
        await self.run_all()
        rep = self.job.doc()["pages"]["0"]
        self.assertEqual(rep["lost_n"], 1)
        self.assertEqual(len([b for b in self.job.doc()["blocks"] if b["page"] == 0]), 1)

    async def test_page_where_nothing_could_be_placed_stays_open(self):
        nowhere = answer([item(1, "paragraph", "Nothing of this is printed anywhere on the page, I made it all up for fun.")])
        self.script([nowhere, answer([item(1, "heading", HEAD)])])
        meta = await self.run_all()
        self.assertEqual(pipeline.page_states(self.job.doc()), [0, 2])
        self.assertEqual(meta["status"], "paused")
        self.assertEqual(meta["progress"]["done"], 1)
        self.assertEqual(self.job.doc()["pages"]["0"]["lost_n"], 1)        # the layout view can still list what was not found

    async def test_api_error_stops_the_job_but_keeps_finished_pages(self):
        from app.llm.clients import LLMError
        self.script([answer([item(1, "heading", HEAD)]), LLMError("HTTP 401: bad key")])
        meta = await self.run_all()
        self.assertEqual(meta["status"], "error")
        self.assertIn("401", meta["error"])
        self.assertEqual(pipeline.page_states(self.job.doc()), [2, 0])

    async def test_translate_only_open_pages_and_reset(self):
        self.script([answer([item(1, "heading", HEAD)]), answer([item(1, "heading", HEAD)])])
        await self.run_all()
        self.assertEqual(pipeline.pages_to_do(self.job.doc(), None), [])
        self.assertEqual(pipeline.pages_to_do(self.job.doc(), {0}), [])


if __name__ == "__main__":
    unittest.main()
