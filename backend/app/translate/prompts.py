"""Prompt construction and response parsing. Everything here is plain text."""
from __future__ import annotations

import re

_T_RE = re.compile(r'<t\s+id="(\d+)"[^>]*>(.*?)</t>', re.S)
_TERM_RE = re.compile(r'<term\s+src="(.*?)"\s*>(.*?)</term>', re.S)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)


def translate_system(lang: str) -> str:
    return f"""You are a professional document translator working inside a PDF translation pipeline.
Translate the text of every element into {lang}.

Input: a list of <t id="N" type="...">source text</t> elements in reading order. Each element is ONE flowing piece of text printed at its own place on a page: a heading, a paragraph or part of one, a list_item, a table cell or label, a running page header/footer (type="header"/"footer"), a caption. The type is only a hint. Elements are short and often fragments (a table of contents row, a form label, one cell): translate each on its own, never merge or split elements. Use the neighbouring elements, the background notes and the earlier translations to translate each unit correctly. The page is re-typeset from your translations, so line breaks do not matter: never add any.

Output rules:
1. Reply with exactly one <t id="N">translation</t> element for EVERY input element, with the same ids and in the same order. No commentary, no markdown fences, nothing outside the tags.
2. Keep inline markup exactly: **bold** stays **bold** (bold the corresponding words), *emphasis* stays *emphasis*, and links [text](https://url) stay links — translate the text inside the brackets but never change the URL. If the link text is itself a URL, keep it unchanged. A literal asterisk is written \\*.
3. Do not translate or alter numbers, units, formulas, code, URLs, e-mail addresses, citation markers such as [12], or identifiers. Keep proper nouns and brand names in their original form unless an established {lang} translation exists.
4. Keep headings and table cells concise and in the same style as the source (a noun phrase stays a noun phrase; do not add a final period that the source does not have).
5. Be consistent with the glossary and the earlier translations. Use glossary terms exactly as given.
6. If an element is already in {lang} or has nothing to translate, return it unchanged.
7. After the translations, add one <term src="original term">translated term</term> element for each specialised term, abbreviation, name or title whose translation you had to decide in this request and that is likely to appear again in the document (technical vocabulary, jargon, names). Skip everyday words and terms already in the glossary. Omit this if there are none.
"""


def translate_user(summary: str, glossary: str, recent: str, page_no: int, page_total: int,
                   items: list[tuple[int, str, str]]) -> str:
    # PROMPT-CACHE FRIENDLY ORDER: providers cache the longest identical prefix. Everything that stays the
    # same between requests (system prompt, summary, the append-only history) comes first; the glossary
    # (chosen per batch) and the batch itself come last. Measured on api.meta.ai: glossary BEFORE the history
    # -> 0% cache hits, history BEFORE the glossary -> ~98%.
    parts = []
    if summary:
        parts.append(f"<background>\n{summary}\n</background>")
    if recent:
        parts.append(f"<earlier_translations>\n{recent}\n</earlier_translations>")
    if glossary:
        parts.append(f"<glossary>\n{glossary}\n</glossary>")
    body = "\n".join(f'<t id="{i}" type="{kind}">{t.replace("</t>", "< /t>")}</t>' for i, kind, t in items)
    parts.append(f'<document_part page="{page_no}" of="{page_total}">\n{body}\n</document_part>')
    return "\n\n".join(parts)


def compress_system() -> str:
    return (
        "You maintain the working memory of a long document translation job. "
        "You receive the previous background notes and the newest source/translation pairs, and you "
        "write updated background notes that let a translator who sees NOTHING else continue the "
        "document consistently."
    )


def compress_user(prev_summary: str, recent: str, lang: str, budget_tokens: int) -> str:
    return f"""<previous_background>
{prev_summary or "(none yet)"}
</previous_background>

<newest_translations>
{recent}
</newest_translations>

Write the updated background notes in English, at most about {budget_tokens} tokens. Cover:
- document type, subject, intended audience and overall structure
- what the parts translated so far covered, and anything left open
- important people, organisations, products and concepts and exactly how they were rendered in {lang}
- style decisions for {lang}: register/politeness level, tone, spelling and punctuation conventions, how headings and captions are phrased
Drop details that no longer matter. Quote {lang} renderings exactly.

Reply with <summary>...</summary>. You may additionally add <term src="source term">translation</term> elements for specialised terms from the newest translations that are worth remembering."""


def parse_translations(text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in _T_RE.finditer(text):
        out[int(m.group(1))] = m.group(2).strip("\n").strip()
    return out


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
