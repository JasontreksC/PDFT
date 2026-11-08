"""Cheap token estimation. The pipeline calibrates it against the `usage` numbers
the provider reports (see Context.calibrate), so it only has to be roughly right."""
import re

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯＀-￯]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return int(cjk * 1.1 + (len(text) - cjk) / 3.6) + 1
