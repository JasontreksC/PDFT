"""Inline markup (**bold**, [text](url)) -> HTML for MuPDF Story, plus line-breaking helpers.

MuPDF breaks Hangul between ANY two syllables and ignores `word-break: keep-all`; gluing the syllables of
a word with U+2060 WORD JOINER makes it break at spaces only (verified). Long URLs get U+200B break points.
"""
from __future__ import annotations

import html
import re

_HANGUL_PAIR = re.compile(r"([\uac00-\ud7a3])(?=[\uac00-\ud7a3])")
_LONG_TOKEN = re.compile(r"\S{28,}")
_TOKEN = re.compile(
    r"(?P<esc>\\\*)"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)"
    r"|\*\*\*(?P<bi>.+?)\*\*\*"
    r"|\*\*(?P<b>.+?)\*\*"
    r"|\*(?P<i>[^*\s][^*]*?)\*",
    re.S,
)


def parse_inline(text: str, bold: bool = False, url: str | None = None) -> list[tuple[str, bool, str | None]]:
    """-> [(text, bold, url)]. Italic markers are dropped (the fonts have no italic face)."""
    out: list[tuple[str, bool, str | None]] = []
    pos = 0
    for m in _TOKEN.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], bold, url))
        if m.group("esc"):
            out.append(("*", bold, url))
        elif m.group("ltext") is not None:
            out.extend(parse_inline(m.group("ltext"), bold, m.group("url")))
        elif m.group("bi") is not None:
            out.extend(parse_inline(m.group("bi"), True, url))
        elif m.group("b") is not None:
            out.extend(parse_inline(m.group("b"), True, url))
        else:
            out.extend(parse_inline(m.group("i"), bold, url))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], bold, url))
    return out


def plain_text(text: str) -> str:
    return "".join(t for t, _, _ in parse_inline(text))


def _breakable(s: str) -> str:
    s = _HANGUL_PAIR.sub("\\1\u2060", s)

    def url_breaks(m: re.Match) -> str:
        return re.sub(r"([/&?=_.\-,])(?=\S)", "\\1\u200b", m.group(0))

    return _LONG_TOKEN.sub(url_breaks, s)


def to_html(text: str, bold_face: str | None, link_color: str = "#1155cc", link_tags: bool = True,
            breakable: bool = True) -> str:
    """Escape + inline markup. `bold_face` is the @font-face family used for **bold** runs.
    link_tags=False draws links (colour + underline) without <a>, for callers that add one clean link themselves."""
    parts = []
    for t, b, url in parse_inline(text):
        h = html.escape(_breakable(t) if breakable else t, quote=False).replace("\n", "<br>")
        if b and bold_face:
            h = f'<span style="font-family:\'{bold_face}\'">{h}</span>'
        if url and link_tags:
            h = f'<a href="{html.escape(url, quote=True)}" style="color:{link_color};text-decoration:underline">{h}</a>'
        elif url:
            h = f'<span style="color:{link_color};text-decoration:underline">{h}</span>'
        parts.append(h)
    return "".join(parts)


def has_bold(text: str) -> bool:
    return any(b for _, b, _ in parse_inline(text))
