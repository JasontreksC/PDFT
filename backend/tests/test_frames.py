"""Frames are cut to exactly the matched characters (table of contents: no dot leader, no page number)."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.frames import build as fb


def toc_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=300)
    y = 60
    for num, title, pg in [("1.", "INTRODUCTION", "5"), ("2.1", "GLOSSARY OF TERMS", "6")]:
        page.insert_text((30, y), num, fontsize=11)                      # the number is its own segment (a gap follows)
        page.insert_text((70, y), title, fontsize=11)
        page.insert_text((70 + 11 * len(title) * 0.62 + 4, y), "." * 45, fontsize=11)     # dot leader
        page.insert_text((460, y), pg, fontsize=11)                      # page number
        y += 30
    page.insert_textbox((30, 200, 470, 260), "A normal paragraph that wraps over two lines because it is quite long for this narrow box, really.", fontsize=11)
    doc.save(path)


class TocTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "toc.pdf"
        toc_pdf(self.pdf)
        self.pi = fb.read_page(self.pdf, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, originals):
        items = [{"id": i + 1, "layout": "heading", "original": o, "translation": "번역 " + o[:6]} for i, o in enumerate(originals)]
        return fb.frames_from_items(self.pi, items)

    def test_frame_stops_at_the_title_and_the_leader_stays(self):
        r = self.build(["1. INTRODUCTION"])
        f = r["frames"][0]
        self.assertEqual(f["n_lines"], 1)
        self.assertLess(f["bbox"][2], 160)                                  # the dots start further right
        self.assertEqual(f["src"], "1. INTRODUCTION")                       # number and title are ONE line (two PDF segments)
        self.assertEqual(f["align"], "left")
        right = [u for u in r["untouched"] if u[0] > 120 and u[1] < 70]    # leader + page number are left standing
        self.assertTrue(right, r["untouched"])
        self.assertGreater(max(u[2] for u in right), 455)                   # ... up to the page number

    def test_model_alignment_wins_for_a_single_line(self):
        for given, want in [("right", "right"), ("Centered", "center"), ("justify", "left"), ("diagonal", "left"), (None, "left")]:
            items = [{"id": 1, "layout": "heading", "original": "1. INTRODUCTION", "translation": "서론", "align": given}]
            self.assertEqual(fb.frames_from_items(self.pi, items)["frames"][0]["align"], want, given)

    def test_measured_paragraph_edges_are_not_overruled(self):
        para = "A normal paragraph that wraps over two lines because it is quite long for this narrow box, really."
        items = [{"id": 1, "layout": "paragraph", "original": para, "translation": "번역", "align": "right"}]
        f = fb.frames_from_items(self.pi, items)["frames"][0]
        self.assertGreaterEqual(f["n_lines"], 2)
        self.assertEqual(f["align"], "left")                    # its lines start at one x: that was measured
        self.assertNotIn("_sure", f)

    def test_every_entry_gets_its_own_row_frame(self):
        r = self.build(["1. INTRODUCTION", "2.1 GLOSSARY OF TERMS"])
        self.assertEqual(len(r["frames"]), 2)
        self.assertLess(r["frames"][1]["bbox"][2], 200)
        self.assertEqual(r["report"]["lost_n"], 0)

    def test_whole_paragraph_lines_are_kept_as_printed(self):
        r = self.build(["A normal paragraph that wraps over two lines because it is quite long for this narrow box, really."])
        f = r["frames"][0]
        self.assertGreaterEqual(f["n_lines"], 2)
        self.assertTrue(all(ln[0] <= 31 for ln in f["lines"]))


if __name__ == "__main__":
    unittest.main()


def figures_pdf(path: Path) -> None:
    """A list of figures: label | caption (hanging second line) | dot leader | page number."""
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=300)
    for y, label, cap, pg in [(60, "Figure 1.", ["Hexadecimal representation of bit patterns"], "8"),
                              (90, "Figure 10.", ["AddRoundKey() XORs each column of the State with a", "word from the key schedule"], "19")]:
        page.insert_text((30, y), label, fontsize=11)
        page.insert_text((100, y), cap[0], fontsize=11)
        if len(cap) == 1:
            page.insert_text((100 + 5.2 * len(cap[0]) + 4, y), "." * 30, fontsize=11)
        else:
            page.insert_text((100, y + 13), cap[1], fontsize=11)
            page.insert_text((100 + 5.2 * len(cap[1]) + 4, y + 13), "." * 30, fontsize=11)
        page.insert_text((460, y if len(cap) == 1 else y + 13), pg, fontsize=11)
    doc.save(path)


class LabelTest(unittest.TestCase):
    def test_label_and_caption_are_separate_frames(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "f.pdf"
            figures_pdf(pdf)
            items = [("label", ["Figure 1."]), ("caption", ["Hexadecimal representation of bit patterns"]),
                     ("label", ["Figure 10."]), ("caption", ["AddRoundKey() XORs each column of the State with a", "word from the key schedule"])]
            r = fb.frames_from_items(fb.read_page(pdf, 0), [{"id": i, "layout": l, "lines": ln, "translation": "번역", "align": "left"}
                                                            for i, (l, ln) in enumerate(items)])
        self.assertEqual(r["report"]["lost_n"], 0, r["report"])
        by = {f["src"]: f for f in r["frames"]}
        self.assertEqual(len(r["frames"]), 4)
        self.assertLess(by["Figure 1."]["bbox"][2], 95)                           # the label is a box of its own ...
        cap = by["Hexadecimal representation of bit patterns"]
        self.assertGreater(cap["bbox"][0], 95)                                    # ... and the caption starts after the gap
        self.assertLess(cap["bbox"][2], 330)                                      # ... and stops before the dot leader
        two = next(f for f in r["frames"] if f["src"].startswith("AddRoundKey"))
        self.assertEqual(two["n_lines"], 2)


def numbered_list_pdf(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=300)
    page.insert_text((60, 60), "2.", fontsize=10)
    page.insert_text((90, 60), "Definitions of terms, acronyms, and algorithm parameters;", fontsize=10)
    page.insert_text((60, 90), "3.", fontsize=10)
    page.insert_text((90, 90), "Notation and conventions used in the algorithm specification,", fontsize=10)
    page.insert_text((90, 103), "including the ordering and numbering of bits.", fontsize=10)
    doc.save(path)


class ExcludedNumberTest(unittest.TestCase):
    """A list number the model did not copy is 'excluded text': it stays printed, the frame must not swallow it."""

    def test_number_in_front_of_the_text_stays_out_of_the_frame(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "l.pdf"
            numbered_list_pdf(pdf)
            items = [{"id": 1, "layout": "list_item", "lines": ["Definitions of terms, acronyms, and algorithm parameters;"]},
                     {"id": 2, "layout": "list_item", "lines": ["Notation and conventions used in the algorithm specification,",
                                                                "including the ordering and numbering of bits."]}]
            r = fb.frames_from_items(fb.read_page(pdf, 0), [dict(i, translation="번역", align="left") for i in items])
        self.assertEqual(r["report"]["lost_n"], 0, r["report"])
        self.assertEqual(len(r["frames"]), 2)
        for f in r["frames"]:
            self.assertGreater(f["bbox"][0], 85, f)                      # starts after the number ("2." sits at x 60-70)
            self.assertFalse(f["src"][0].isdigit(), f["src"])
        self.assertEqual(r["frames"][1]["n_lines"], 2)
        self.assertEqual(r["frames"][1]["indent"], 0.0)
        # both numbers are left over: printed text that is not redacted and counts as an obstacle
        self.assertEqual(sum(1 for b in r["untouched"] if b[2] < 80), 2, r["untouched"])


class RedactionTest(unittest.TestCase):
    """Erasing a tightly set paragraph must not take the list number printed in front of its first line."""

    def test_number_above_the_second_line_survives_the_erasing(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "r.pdf"
            doc = pymupdf.open()
            page = doc.new_page(width=500, height=300)
            page.insert_text((60, 60), "3.", fontsize=12)
            page.insert_text((100, 60), "Explanation. The Advanced Encryption Standard specifies", fontsize=12)
            page.insert_text((60, 72), "an approved algorithm that can be used to protect data.", fontsize=12)   # pitch 12 < box height
            doc.save(pdf)
            pi = fb.read_page(pdf, 0)
            self.assertGreater(pi.lines[0].bbox[3], pi.lines[-1].bbox[1])          # the boxes do overlap (the premise)
            r = fb.frames_from_items(pi, [{"id": 1, "layout": "list_item", "translation": "번역", "align": "left", "lines": [
                "Explanation. The Advanced Encryption Standard specifies", "an approved algorithm that can be used to protect data."]}])
            f = r["frames"][0]
            self.assertEqual(f["n_lines"], 2)
            out = pymupdf.open(pdf)
            pg = out[0]
            for lb in f["cut"]:                                 # exactly what compose does
                pg.add_redact_annot(pymupdf.Rect(lb) + (0.4, 0.4, -0.4, -0.4), fill=False)
            pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            self.assertEqual(pg.get_text().split(), ["3."])     # the number stays, the paragraph is gone


class LeaderTest(unittest.TestCase):
    """The model copied the dot leader and the page number although it should not: they still stay out of the frame."""

    def run_toc(self, items):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "toc.pdf"
            toc_pdf(pdf)
            return fb.frames_from_items(fb.read_page(pdf, 0), [dict(i, translation="번역", align="left") for i in items])

    def test_copied_leader_and_page_number_are_not_part_of_the_frame(self):
        r = self.run_toc([{"id": 1, "layout": "list_item", "lines": ["2.1 GLOSSARY OF TERMS " + "." * 45 + " 6"]}])
        self.assertEqual(r["report"]["lost_n"], 0, r["report"])
        f = r["frames"][0]
        self.assertEqual(f["src"], "2.1 GLOSSARY OF TERMS")
        self.assertLess(f["bbox"][2], 300)                      # stops at the title: the leader and the number stay printed

    def test_a_lone_page_number_is_ignored_not_lost(self):
        r = self.run_toc([{"id": 1, "layout": "other", "lines": ["6"]}, {"id": 2, "layout": "other", "lines": ["." * 40]},
                          {"id": 3, "layout": "heading", "lines": ["INTRODUCTION"]}])
        self.assertEqual(r["report"]["lost_n"], 0, r["report"])
        self.assertEqual(r["report"]["items"], 1)               # the two non-texts do not count as items
        self.assertEqual([f["src"] for f in r["frames"]], ["INTRODUCTION"])


class AlignmentTest(unittest.TestCase):
    @staticmethod
    def frame(x0, x1, y):
        return {"bbox": [x0, y, x1, y + 12], "lines": [[x0, y, x1, y + 12]], "n_lines": 1, "indent": 0.0, "size": 10, "align": "left"}

    def test_full_width_line_among_justified_paragraphs_stays_left(self):
        # list items start at x=90; the paragraphs around them end at 543. The item that fills its line ends at 543 too.
        items = [self.frame(90, 509, 100), self.frame(90, 452, 130), self.frame(90, 543, 160), self.frame(90, 543, 190)]
        paras = [self.frame(72, 543, 220 + 30 * k) for k in range(6)]
        fb.infer_alignment(items + paras, 612)
        self.assertEqual([f["align"] for f in items], ["left"] * 4)

    def test_right_aligned_label_is_still_found(self):
        a, b, c = self.frame(400, 543, 10), self.frame(430, 543, 40), self.frame(380, 543, 70)
        fb.infer_alignment([a, b, c], 612)
        self.assertEqual([f["align"] for f in (a, b, c)], ["right"] * 3)


def table_pdf(path: Path, with_left_second_line: bool = True) -> None:
    """A two-column glossary stored ROW BY ROW like many PDFs do: left line 1, right line 1, left line 2, right line 2."""
    doc = pymupdf.open()
    page = doc.new_page(width=500, height=300)
    for y, (l1, r1, l2, r2) in [(60, ("AES", "Advanced Encryption Standard", None, None)),
                                (100, ("Affine", "A transformation consisting of multiplication by a matrix followed by", "Transformation",
                                       "the addition of a vector.")),
                                (160, ("Bit", "A binary digit having a value of 0 or 1.", None, None))]:
        page.insert_text((30, y), l1, fontsize=11)
        page.insert_text((200, y), r1, fontsize=11)
        if l2 and (with_left_second_line or l2 != "Transformation"):
            page.insert_text((30, y + 14), l2, fontsize=11)
        if r2:
            page.insert_text((200, y + 14), r2, fontsize=11)
    doc.save(path)


class TableTest(unittest.TestCase):
    def frames(self, left_second_line=True, items=None, lines=None):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "t.pdf"
            table_pdf(pdf, left_second_line)
            pi = fb.read_page(pdf, 0)
            items = items or [
                ("label", "AES"), ("label", "Advanced Encryption Standard"),
                ("label", "Affine Transformation"),
                ("paragraph", "A transformation consisting of multiplication by a matrix followed by the addition of a vector."),
                ("label", "Bit"), ("paragraph", "A binary digit having a value of 0 or 1.")]
            return fb.frames_from_items(pi, [{"id": i, "layout": l, "original": o, "translation": "번역", "align": "left",
                                              **({"lines": lines[i]} if lines and i in lines else {})}
                                             for i, (l, o) in enumerate(items)])

    def test_cells_stored_row_by_row_still_match_cell_by_cell(self):
        r = self.frames()
        self.assertEqual(r["report"]["lost_n"], 0, r["report"]["lost"])
        by_src = {f["src"]: f for f in r["frames"]}
        label = by_src["Affine Transformation"]                       # both lines of the LEFT cell ...
        self.assertEqual(label["n_lines"], 2)
        self.assertLess(label["bbox"][2], 150)
        text = next(f for f in r["frames"] if f["src"].startswith("A transformation"))    # ... and the whole right cell
        self.assertEqual(text["n_lines"], 2)
        self.assertGreater(text["bbox"][0], 190)

    def test_the_models_line_breaks_decide_between_equal_texts(self):
        r = self.frames(items=[
            ("label", "Affine Transformation"),
            ("paragraph", "A transformation consisting of multiplication by a matrix followed by the addition of a vector.")],
            lines={0: ["Affine", "Transformation"]})
        label = next(f for f in r["frames"] if f["src"].startswith("Affine"))
        self.assertEqual(label["n_lines"], 2)
        self.assertLess(label["bbox"][2], 150)

    def test_wrong_line_breaks_are_only_a_hint(self):
        r = self.frames(lines={2: ["Affine Transformation"]})            # the model put the two lines into one
        self.assertEqual(r["report"]["lost_n"], 0, r["report"]["lost"])
        label = next(f for f in r["frames"] if f["src"].startswith("Affine"))
        self.assertEqual(label["n_lines"], 2)

    def test_a_word_of_the_neighbouring_cell_is_not_borrowed(self):
        # the left cell has no second line: "Transformation" exists only inside the right cell's sentence
        r = self.frames(left_second_line=False, items=[
            ("label", "Affine Transformation"),
            ("paragraph", "A transformation consisting of multiplication by a matrix followed by the addition of a vector.")])
        srcs = [f["src"] for f in r["frames"]]
        self.assertFalse(any(s.startswith("Affine") and s.lower().count("transformation") for s in srcs), srcs)
        self.assertTrue(any(s.startswith("A transformation") for s in srcs))        # the sentence keeps its own words
        self.assertEqual(r["report"]["lost_n"], 1)                                    # the label is reported, not mangled


PARA = ["The block cipher operates on blocks of", "sixteen bytes and uses a key of one", "hundred twenty eight bits to produce",
        "an output of the same size, mixing", "every byte of the state in each round."]


def two_column_pdf(path: Path, right_first=False) -> None:
    """One paragraph that runs down the bottom of the left column and goes on at the top of the right one."""
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=300)
    left = [(20, 220 + 14 * i, t) for i, t in enumerate(PARA[:3])]
    right = [(220, 40 + 14 * i, t) for i, t in enumerate(PARA[3:])]
    for x, y, t in (right + left) if right_first else (left + right):
        page.insert_text((x, y), t, fontsize=10)
    page.insert_text((20, 40), "A heading that belongs to the left column", fontsize=10)
    doc.save(path)


class ColumnTest(unittest.TestCase):
    def run_item(self, with_lines=True, right_first=False):
        with tempfile.TemporaryDirectory() as d:
            pdf = Path(d) / "c.pdf"
            two_column_pdf(pdf, right_first)
            pi = fb.read_page(pdf, 0)
            item = {"id": 1, "layout": "paragraph", "original": " ".join(PARA), "translation": "블록 암호는 열여섯 바이트 블록 단위로 동작하며 한 라운드마다 상태의 모든 바이트를 섞는다.",
                    "align": "left"}
            if with_lines:
                item["lines"] = PARA
            return fb.frames_from_items(pi, [item])

    def test_paragraph_that_continues_in_the_next_column_becomes_one_frame_per_column(self):
        for right_first in (False, True):                               # the order the PDF stores the lines in does not matter
            r = self.run_item(right_first=right_first)
            self.assertEqual(r["report"]["lost_n"], 0, r["report"]["lost"])
            self.assertEqual(len(r["frames"]), 2)
            a, b = sorted(r["frames"], key=lambda f: f["bbox"][0])
            self.assertEqual((a["n_lines"], b["n_lines"]), (3, 2))
            self.assertLess(a["bbox"][2], 215)
            self.assertGreater(b["bbox"][0], 215)
            self.assertEqual(" ".join(f["tgt"] for f in sorted(r["frames"], key=lambda f: f["bbox"][0])).split(),
                             "블록 암호는 열여섯 바이트 블록 단위로 동작하며 한 라운드마다 상태의 모든 바이트를 섞는다.".split())

    def test_without_the_models_line_breaks_a_jump_to_another_column_is_refused(self):
        r = self.run_item(with_lines=False)
        self.assertEqual(r["report"]["lost_n"], 1)
        self.assertEqual(r["frames"], [])


class FlowTest(unittest.TestCase):
    """A frame with printed text on its rows (the list number in front of its first line) keeps its place in the flow."""

    def test_number_inside_the_box_pins_the_frame(self):
        from app.frames.compose import _movable
        f = {"bbox": [72, 538, 542, 582], "style": {"size": 12}}            # "6.  Applicability. ..." : lines 2+ start under the number
        self.assertFalse(_movable(f, [f], [[72, 538, 80, 554]], []))        # "6." left printed, inside the box
        self.assertTrue(_movable(f, [f], [[72, 600, 80, 616]], []))         # printed text elsewhere does not matter
        self.assertFalse(_movable(f, [f], [[548, 540, 560, 552]], []))      # beside it, as before
