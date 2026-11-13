"""Prompt construction and response parsing. Everything here is plain text."""
from __future__ import annotations

import re

_T_RE = re.compile(r'<t\s+id="(\d+)"[^>]*>(.*?)</t>', re.S)
_TERM_RE = re.compile(r'<term\s+src="(.*?)"\s*>(.*?)</term>', re.S)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)

LAYOUTS = "heading, paragraph, list_item, caption, label, footnote, header, footer, other"


def translate_system(lang: str) -> str:
    return f"""You are a professional document translator working inside a PDF translation pipeline.
You receive the screenshot of ONE page of a document. Pick out the text on it that is worth translating and translate it into {lang}.

WHAT TO EXTRACT
- Only text that should be translated: headings, paragraphs, list items, captions, footnotes, the words of figure and table labels, running page headers/footers that contain words.
- Table of contents, index and list-of-figures lines: give only what is worth translating, the number or label at the start and the title. Leave out the dot leader ("........") and the page number at the end of the line.
- Leave out everything that must not be translated: formulas and equations, bare numbers, symbols, code, table cells that hold only numbers, a lone page number, dot leaders and rules made of characters, a lone URL or e-mail address, bare identifiers.
- Split along the layout, by visual blocks: one item per heading, paragraph, list item, caption, label or table cell. A paragraph is ONE item however many lines it takes. Never join text of different blocks or table cells, and never split one paragraph. A paragraph that continues at the top of the next column is still one item. List the items in reading order.
- A wide blank gap inside a line (a tab or a column alignment) separates two different items: what stands before the gap and what stands after it are separate items.
- In lists, tables of contents and lists of figures or tables, a number or label at the start of a line ("Figure 1.", "2.1", "(a)", "•") that is set apart from the text after it by such a gap is an item of its own with layout "label"; the text after it is a "list_item" (a "caption" in a list of figures or tables). When that text goes on in the following lines (hanging indent), those lines belong to the "lines" of the text item.
  Example: the line "Figure 1.   Hexadecimal representation of bit patterns ....... 8" of a list of figures gives two items: {{"id": 2, "layout": "label", "lines": ["Figure 1."], "translation": "..."}} and {{"id": 3, "layout": "caption", "lines": ["Hexadecimal representation of bit patterns"], "translation": "..."}}.

FIELDS OF AN ITEM
- "id": 1, 2, 3 ... in order.
- "layout": one of {LAYOUTS}.
- "align": how the text sits in its place on the page: "left", "center" or "right" (justified text counts as "left"). Look at where the text starts and ends relative to the text around it: a heading in the middle of the page is "center", a number or label hugging the right margin is "right", a list item or a paragraph that merely fills its line is "left".
- "lines": ONLY the part of the item that is worth translating, copied from the page, ONE STRING PER PRINTED LINE, top to bottom, the way the lines are broken on the page (a one-line heading is a list with one string). What you do copy must be exact, character for character (keep a hyphen that ends a line); do not correct, reorder, shorten, summarise or translate it. What is not worth translating is NOT copied: a dot leader ("........") and the page number at the end of a table-of-contents line, a bare number, a formula, a symbol. The strings are matched against the PDF's own text and the line breaks tell where each piece of the item sits, so they must be faithful. Write plain text: no markdown, no LaTeX.
- "translation": the translation into {lang}. The page is re-typeset from it, so never add line breaks.

TRANSLATION RULES
1. Translate the meaning of the whole item; use the background notes and the glossary so terms stay consistent. Use glossary terms exactly as given.
2. Inline emphasis: if words inside the item are clearly bold, write them **bold**; clearly italic, *italic*. Do not add emphasis that is not on the page. A literal asterisk is written \\*.
3. Do not translate or alter numbers, units, formulas, code, URLs, e-mail addresses, citation markers such as [12], or identifiers. Keep proper nouns and brand names in their original form unless an established {lang} translation exists.
4. Keep headings, captions and table cells concise and in the same style as the source (a noun phrase stays a noun phrase; no final period that the source does not have).
5. If an item is already in {lang} or has nothing to translate, leave it out.

TERMS
After the items, list in "terms" the specialised terms, abbreviations, names or titles whose translation you had to decide on this page and that are likely to appear again in the document ({{"src": "original term", "tgt": "translated term"}}). Skip everyday words and terms already in the glossary. Use an empty list if there are none.

OUTPUT
Reply with ONE valid JSON object and nothing else (no code fence, no commentary; escape quotes and backslashes inside strings):
{{"items": [{{"id": 1, "layout": "paragraph", "align": "left", "lines": ["first printed line", "second printed line"], "translation": "..."}}], "terms": [{{"src": "...", "tgt": "..."}}]}}
If the page has nothing to translate reply {{"items": [], "terms": []}}.
"""


def translate_user(summary: str, glossary: str, page_no: int, page_total: int, resume_after: str | None = None) -> str:
    """The request text that goes with the screenshot. Standing context is ONLY the short background summary and the glossary
    entries that occur on this page. Order is prompt-cache friendly: what stays identical between requests comes first."""
    parts = []
    if summary:
        parts.append(f"<background>\n{summary}\n</background>")
    if glossary:
        parts.append(f"<glossary>\n{glossary}\n</glossary>")
    parts.append(f"Translate the page in the attached screenshot (page {page_no} of {page_total}).")
    if resume_after:
        parts.append("Your previous answer for this page was cut off by the length limit. Continue with the item that comes right after the one "
                     f'whose text ends with: "{resume_after}". Do NOT repeat earlier items. Reply with the same JSON format, ids continuing from where you stopped.')
    return "\n\n".join(parts)


# ------------------------------------------------------------------------------ "기본 번역": text of the frames, no screenshot
def text_system(lang: str) -> str:
    return f"""You are a professional document translator working inside a PDF translation pipeline.
Translate the text of every element into {lang}.

Input: a list of <t id="N" type="...">source text</t> elements in reading order. Each element is ONE flowing piece of text printed at its own place on a page: a heading, a paragraph or part of one, a list_item, a table cell or label, a running page header/footer (type="header"/"footer"), a caption. The type is only a hint. Elements are short and often fragments (a table of contents row, a form label, one cell): translate each on its own, never merge or split elements. Use the neighbouring elements, the background notes and the glossary to translate each unit correctly. The page is re-typeset from your translations, so line breaks do not matter: never add any.

Output rules:
1. Reply with exactly one <t id="N">translation</t> element for EVERY input element, with the same ids and in the same order. No commentary, no markdown fences, nothing outside the tags.
2. Keep inline markup exactly: **bold** stays **bold** (bold the corresponding words), *emphasis* stays *emphasis*, and links [text](https://url) stay links — translate the text inside the brackets but never change the URL. If the link text is itself a URL, keep it unchanged. A literal asterisk is written \\*.
3. Do not translate or alter numbers, units, formulas, code, URLs, e-mail addresses, citation markers such as [12], or identifiers. Keep proper nouns and brand names in their original form unless an established {lang} translation exists.
4. Keep headings and table cells concise and in the same style as the source (a noun phrase stays a noun phrase; do not add a final period that the source does not have).
5. Be consistent with the background notes and the glossary. Use glossary terms exactly as given.
6. If an element is already in {lang} or has nothing to translate, return it unchanged.
7. After the translations, add one <term src="original term">translated term</term> element for each specialised term, abbreviation, name or title whose translation you had to decide in this request and that is likely to appear again in the document (technical vocabulary, jargon, names). Skip everyday words and terms already in the glossary. Omit this if there are none.
"""


def text_user(summary: str, glossary: str, page_no: int, page_total: int, items: list[tuple[int, str, str]]) -> str:
    """The request body of "기본 번역": the background summary, the glossary entries that occur in this batch, then the
    batch. Order is prompt-cache friendly: what stays identical between requests comes first."""
    parts = []
    if summary:
        parts.append(f"<background>\n{summary}\n</background>")
    if glossary:
        parts.append(f"<glossary>\n{glossary}\n</glossary>")
    body = "\n".join(f'<t id="{i}" type="{kind}">{t.replace("</t>", "< /t>")}</t>' for i, kind, t in items)
    parts.append(f'<document_part page="{page_no}" of="{page_total}">\n{body}\n</document_part>')
    return "\n\n".join(parts)


def parse_translations(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in _T_RE.finditer(text):
        out[int(m.group(1))] = m.group(2).strip("\n").strip()
    return out


# ------------------------------------------------------------------------------ the background summary (both modes)
def compress_system() -> str:
    return (
        "You maintain the working memory of a long document translation job. "
        "The translator who continues the document sees NOTHING from earlier pages except your background notes "
        "and a glossary of fixed term translations. You receive the previous notes and an excerpt of the newest "
        "source => translation pairs, and you write the updated notes."
    )


def compress_user(prev_summary: str, recent: str, lang: str, budget_tokens: int) -> str:
    return f"""<previous_background>
{prev_summary or "(none yet)"}
</previous_background>

<newest_translations>
{recent}
</newest_translations>

Write the updated background notes in English. HARD LIMIT: {budget_tokens} tokens - shorter is better, because they are sent with every request. Cover only what a translator needs to continue consistently:
- document type, subject, audience and overall structure; where the translated part has reached
- style decisions for {lang}: register/politeness level, tone, spelling and punctuation conventions, how headings and captions are phrased
- recurring people, organisations and concepts and how they are rendered in {lang} (terms listed as <term> below do not need to be repeated here)
Drop everything else, including details of individual sentences. Quote {lang} renderings exactly.

Reply with <summary>...</summary>. Then add a <term src="source term">translation</term> element for each specialised term, abbreviation, name or title in the newest translations that is likely to come back later in the document (they go into the glossary and are sent to the translator whenever the term occurs)."""


def parse_terms(text: str) -> list[tuple[str, str]]:
    res = []
    for m in _TERM_RE.finditer(text):
        src, tgt = m.group(1).strip(), m.group(2).strip()
        if src and tgt and len(src) <= 120 and len(tgt) <= 200 and "\n" not in src:
            res.append((src, tgt))
    return res


def parse_summary(text: str) -> str:
    m = _SUMMARY_RE.search(text)
    if m:
        return m.group(1).strip()
    cleaned = _TERM_RE.sub("", text).strip()
    return cleaned if len(cleaned) > 20 else ""
