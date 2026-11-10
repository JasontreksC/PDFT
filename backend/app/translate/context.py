"""Internal translation memory of one job: a short background summary + a glossary (+ a buffer of recent pairs).

What a translation request carries from here is ONLY the summary and the glossary entries that occur in the batch.
The buffer of recent source/translation pairs (`history`) is never sent to the translator: it exists to feed the
next summary refresh and is emptied by it.

PRIVATE: this object is persisted under jobs/<id>/internal/ and is never serialised into
any API response. The API only ever exposes token counts derived from it (the gauge).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, fields
from pathlib import Path

from .tokens import estimate_tokens

MAX_PAIR_CHARS = 600
PAIR_EXCERPT = 240      # each side of a pair is clipped to this many characters when it is shown to the summariser


def pairs_text(chunks: list, excerpt: int | None = None, max_tokens: int | None = None) -> str:
    """Render [{"page": n, "pairs": [[src, tgt], ...]}] as text. `excerpt` clips each side of a pair; `max_tokens`
    keeps the NEWEST pairs that fit (the oldest ones are dropped first)."""
    def clip(s: str) -> str:
        s = s.replace("\n", " / ")
        return s if not excerpt or len(s) <= excerpt else s[:excerpt] + "…"

    blocks: list[str] = []
    for ch in chunks:
        lines = [f"{clip(s)} => {clip(t)}" for s, t in ch["pairs"]]
        if not lines:
            continue
        head = f"[page {ch['page']}]\n" if ch.get("page") is not None else ""
        blocks.append(head + "\n".join(lines))
    if max_tokens:
        kept, used = [], 0
        for b in reversed(blocks):
            n = estimate_tokens(b)
            if kept and used + n > max_tokens:
                break
            kept.append(b)
            used += n
        blocks = kept[::-1]
    return "\n".join(blocks)


@dataclass
class Context:
    summary: str = ""
    history: list = field(default_factory=list)    # [{"page": n, "pairs": [[src, tgt], ...]}] since the last summary refresh
    glossary: dict = field(default_factory=dict)   # key -> {"src", "tgt", "n"}
    memo: dict = field(default_factory=dict)       # exact-match cache for short repeated strings
    calib: float = 1.0                             # real_tokens / estimated_tokens (EMA)
    compressions: int = 0                          # summary refreshes so far

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path) -> "Context":
        try:
            raw = json.loads(path.read_text("utf-8"))
            known = {f.name for f in fields(cls)}
            ctx = cls(**{k: v for k, v in raw.items() if k in known})
            if raw.get("cur_pairs"):                  # older files: pairs of a page that was in progress
                ctx.history.append({"page": raw.get("cur_page"), "pairs": raw["cur_pairs"]})
            return ctx
        except (FileNotFoundError, json.JSONDecodeError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    def snapshot(self) -> "Context":
        """What a page worker starts from: summary, glossary, memo - without the pair buffer (never needed to translate)."""
        return Context(summary=self.summary, glossary=copy.deepcopy(self.glossary), memo=dict(self.memo),
                       calib=self.calib, compressions=self.compressions)

    # ------------------------------------------------------------------ pairs (feed for the summary refresh)
    def pending_tokens(self) -> int:
        """Size of the pairs waiting to be folded into the summary (clipped the way the refresh will see them)."""
        return estimate_tokens(pairs_text(self.history, excerpt=PAIR_EXCERPT))

    def trim_pending(self, max_tokens: int) -> None:
        """Bound the buffer when refreshes are off or failing: drop the oldest pairs."""
        flat = [(h.get("page"), p) for h in self.history for p in h["pairs"]]
        total = sum(estimate_tokens(s[:PAIR_EXCERPT] + t[:PAIR_EXCERPT]) + 3 for _, (s, t) in flat)
        while flat and total > max_tokens:
            _, (s, t) = flat.pop(0)
            total -= estimate_tokens(s[:PAIR_EXCERPT] + t[:PAIR_EXCERPT]) + 3
        self.history = [{"page": flat[0][0], "pairs": [p for _, p in flat]}] if flat else []

    # --------------------------------------------------------------- glossary
    def add_terms(self, terms: list[tuple[str, str]]) -> None:
        for src, tgt in terms:
            key = src.lower()
            if key in self.glossary:
                self.glossary[key]["n"] += 1  # first decision wins -> consistency
            else:
                self.glossary[key] = {"src": src, "tgt": tgt, "n": 1}

    def glossary_text(self, batch_text: str, budget_tokens: int) -> str:
        """Glossary entries that occur in this batch, most frequent first, within a token budget."""
        low = batch_text.lower()
        hits = [g for k, g in self.glossary.items() if k in low]
        hits.sort(key=lambda g: -g["n"])
        out, used = [], 0
        for g in hits:
            line = f"{g['src']} => {g['tgt']}"
            t = estimate_tokens(line) + 1
            if used + t > budget_tokens:
                break
            out.append(line)
            used += t
        return "\n".join(out)

    def glossary_text_all(self, budget_tokens: int) -> str:
        items = sorted(self.glossary.values(), key=lambda g: -g["n"])
        out, used = [], 0
        for g in items:
            line = f"{g['src']} => {g['tgt']}"
            t = estimate_tokens(line) + 1
            if used + t > budget_tokens:
                break
            out.append(line)
            used += t
        return "\n".join(out)

    # ------------------------------------------------------------------ gauge
    def raw_tokens(self, system: str, glossary_budget: int) -> int:
        """What a request carries besides the text to translate: system prompt + summary + (at most) the glossary budget."""
        return (
            estimate_tokens(system)
            + estimate_tokens(self.summary)
            + estimate_tokens(self.glossary_text_all(glossary_budget))
        )

    def used_tokens(self, system: str, glossary_budget: int) -> int:
        return int(self.raw_tokens(system, glossary_budget) * self.calib)

    def calibrate(self, estimated: int, actual: int | None) -> None:
        if actual and estimated > 50:
            ratio = max(0.4, min(4.0, actual / estimated))
            self.calib = 0.6 * self.calib + 0.4 * ratio

