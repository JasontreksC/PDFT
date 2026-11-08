"""Internal translation memory of one job: rolling summary + recent pairs + glossary.

PRIVATE: this object is persisted under jobs/<id>/internal/ and is never serialised into
any API response. The API only ever exposes token counts derived from it (the gauge).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .tokens import estimate_tokens

MAX_PAIR_CHARS = 600


@dataclass
class Context:
    summary: str = ""
    history: list = field(default_factory=list)    # [{"page": n, "pairs": [[src, tgt], ...]}] since last compression
    cur_pairs: list = field(default_factory=list)  # pairs of the page currently being translated
    glossary: dict = field(default_factory=dict)   # key -> {"src", "tgt", "n"}
    memo: dict = field(default_factory=dict)       # exact-match cache for short repeated strings
    calib: float = 1.0                             # real_tokens / estimated_tokens (EMA)
    compressions: int = 0
    cur_page: int | None = None                    # page number of cur_pairs (kept so the history text never changes retroactively)

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path) -> "Context":
        try:
            return cls(**json.loads(path.read_text("utf-8")))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------------ pairs
    def add_pair(self, src: str, tgt: str) -> None:
        self.cur_pairs.append([src[:MAX_PAIR_CHARS], tgt[:MAX_PAIR_CHARS]])
        if len(src) <= 200:
            self.memo[src] = tgt

    def end_page(self, page_no: int) -> None:
        if self.cur_pairs:
            self.history.append({"page": page_no, "pairs": self.cur_pairs})
        self.cur_pairs = []

    def history_text(self) -> str:
        chunks = list(self.history)
        if self.cur_pairs:
            chunks.append({"page": self.cur_page, "pairs": self.cur_pairs})
        lines = []
        for ch in chunks:
            if ch["page"] is not None:
                lines.append(f"[page {ch['page']}]")
            lines += [f"{s.replace(chr(10), ' / ')} => {t.replace(chr(10), ' / ')}" for s, t in ch["pairs"]]
        return "\n".join(lines)

    def hard_trim(self, keep: float = 0.3) -> None:
        """Fallback when the LLM could not compress: drop the oldest raw pairs."""
        flat = [(h["page"], p) for h in self.history for p in h["pairs"]]
        flat = flat[int(len(flat) * (1 - keep)):]
        self.history = [{"page": flat[0][0] if flat else 0, "pairs": [p for _, p in flat]}] if flat else []

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
        return (
            estimate_tokens(system)
            + estimate_tokens(self.summary)
            + estimate_tokens(self.history_text())
            + estimate_tokens(self.glossary_text_all(glossary_budget))
        )

    def used_tokens(self, system: str, glossary_budget: int) -> int:
        """Tokens the standing context (everything but the page text) would occupy in a request."""
        return int(self.raw_tokens(system, glossary_budget) * self.calib)

    def calibrate(self, estimated: int, actual: int | None) -> None:
        if actual and estimated > 50:
            ratio = max(0.4, min(4.0, actual / estimated))
            self.calib = 0.6 * self.calib + 0.4 * ratio
