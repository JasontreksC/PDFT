"""Matcher checks on a synthetic PDF (no network, no LLM).   cd backend && python -m unittest tests.test_match
(needs only pymupdf)"""
import unittest

import pymupdf

from app.match.llm_json import parse_items
from app.match.matcher import PageText, match_items

P1 = ("The quick brown fox jumps over the lazy dog while the encryption algorithm transforms every single block "
      "of the message into something unreadable.")
P2 = "A second paragraph that talks about completely different matters such as hyphenation and typography rules."
TITLE = "3.4 The Sample Heading"


def make_page() -> pymupdf.Page:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.insert_textbox((20, 20, 380, 50), TITLE, fontsize=14, fontname="hebo")
    page.insert_textbox((20, 60, 200, 170), P1, fontsize=10)                    # left column, wraps over several lines
    page.insert_textbox((210, 60, 380, 170), P2, fontsize=10)                   # right column
    page.insert_textbox((20, 200, 380, 230), "The quick brown fox jumps over the lazy dog", fontsize=10)   # duplicate of P1's start
    page.insert_textbox((20, 260, 380, 280), "s_{r,c} = in[r + 4c]  (3.3)", fontsize=10)   # equation: never listed by the LLM
    return page


def run(items, thr=0.9):
    pt = PageText.from_page(make_page())
    return match_items(pt, [{"id": i + 1, "original": t} for i, t in enumerate(items)], thr)


class MatcherTest(unittest.TestCase):
    def test_exact_and_box_inside_column(self):
        r = run([P1])["items"][0]
        self.assertGreaterEqual(r["score"], 0.99)
        x0, y0, x1, y1 = r["bbox"]
        self.assertLess(x1, 205)                  # stays in the left column
        self.assertGreater(len(r["line_boxes"]), 2)

    def test_typos_and_formatting_still_match(self):
        noisy = P1.replace("encryption", "encripton").replace("quick", "QUICK").replace(" over ", "  over\n")
        r = run([noisy])["items"][0]
        self.assertTrue(r["matched"], r["score"])
        self.assertLess(r["score"], 1.0)

    def test_markdown_and_latex_noise_is_ignored(self):
        r = run(["**3.4 The Sample Heading**"])["items"][0]
        self.assertGreaterEqual(r["score"], 0.99)

    def test_unrelated_text_is_below_threshold(self):
        r = run(["Completely invented sentence that is nowhere on this page at all, honestly."])["items"][0]
        self.assertFalse(r["matched"])

    def test_duplicates_are_assigned_to_different_places(self):
        res = run(["The quick brown fox jumps over the lazy dog", "The quick brown fox jumps over the lazy dog"])["items"]
        self.assertTrue(all(r["matched"] for r in res))
        self.assertNotEqual(res[0]["bbox"], res[1]["bbox"])

    def test_two_columns_do_not_mix(self):
        a, b = run([P1, P2])["items"]
        self.assertLess(a["bbox"][2], b["bbox"][0] + 1)

    def test_split_fallback_for_joined_labels(self):
        res = run([TITLE + " / " + P2[:30]])["items"][0]
        self.assertTrue(res["matched"], res["score"])
        self.assertTrue(res["split"])

    def test_a_long_dot_leader_in_the_original_does_not_break_the_match(self):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=200)
        page.insert_text((30, 60), "2.1 Glossary of terms", fontsize=11)
        page.insert_text((150, 60), "." * 50, fontsize=11)
        page.insert_text((360, 60), "6", fontsize=11)
        r = match_items(PageText.from_page(page), [{"id": 1, "original": "2.1 Glossary of terms " + "." * 50 + " 6"}])["items"][0]
        self.assertTrue(r["matched"], r)

    def test_coverage_marks_equation_line_uncovered(self):
        r = run([TITLE, P1, P2, "The quick brown fox jumps over the lazy dog"])
        self.assertTrue(any("(3.3)" in u["text"] for u in r["uncovered"]))
        self.assertGreater(r["coverage"], 0.8)


class ParseTest(unittest.TestCase):
    def test_nested_and_fenced(self):
        txt = '```json\n{"document":{"page":1},"extracted_texts":[{"id":1,"original":"Hello","korean_translation":"안녕"}],' \
              '"excluded_items":{"items":["x = 1"]}}\n```'
        items, warn = parse_items(txt)
        self.assertEqual([i["original"] for i in items], ["Hello"])
        self.assertEqual(items[0]["translation"], "안녕")
        self.assertIsNone(warn)

    def test_alignment_is_read(self):
        from app.match.llm_json import parse_page
        items, terms, warn, ok = parse_page('{"items":[{"id":1,"layout":"heading","align":"center","original":"Title","translation":"제목"},'
                                            '{"id":2,"original":"Body","translation":"본문"}]}')
        self.assertEqual([i["align"] for i in items], ["center", ""])

    def test_truncated_json_is_salvaged(self):
        txt = '{"items":[{"original":"One","translation":"하나"},{"original":"Two","translation":"둘"},{"original":"Thr'
        items, warn = parse_items(txt)
        self.assertEqual([i["original"] for i in items], ["One", "Two"])
        self.assertIsNotNone(warn)

    def test_lines_are_read_and_joined_into_the_original(self):
        items, warn = parse_items('{"items":[{"id":1,"lines":["Affine","Transformation"],"translation":"아핀 변환"},'
                                  '{"id":2,"original":"Body text","lines":["Body","text"],"translation":"본문"},'
                                  '{"id":3,"lines":"one\\ntwo","translation":"x"}]}')
        self.assertEqual([i["original"] for i in items], ["Affine Transformation", "Body text", "one two"])
        self.assertEqual([i["lines"] for i in items], [["Affine", "Transformation"], ["Body", "text"], ["one", "two"]])
        self.assertIsNone(warn)

    def test_truncated_lines_answer_is_salvaged(self):
        txt = '{"items":[{"lines":["One","more"],"translation":"하나"},{"lines":["Two"],"translation":"둘"},{"lines":["Thr'
        items, warn = parse_items(txt)
        self.assertEqual([i["lines"] for i in items], [["One", "more"], ["Two"]])
        self.assertIsNotNone(warn)

    def test_garbage(self):
        self.assertEqual(parse_items("sorry, I cannot")[0], [])


if __name__ == "__main__":
    unittest.main()
