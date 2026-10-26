"""BM25 keyword retrieval over the FAQ. Returns a pre-written answer; nothing is generated."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from app.rules import FaqEntry

STOP = {
    # grammar
    "the",
    "a",
    "an",
    "is",
    "are",
    "do",
    "does",
    "you",
    "your",
    "i",
    "we",
    "to",
    "of",
    "for",
    "my",
    "me",
    "can",
    "have",
    "what",
    "when",
    "where",
    "how",
    "which",
    "there",
    "it",
    "in",
    "on",
    "at",
    "and",
    "or",
    "be",
    "will",
    "would",
    "get",
    "please",
    "tell",
    "about",
    "if",
    "that",
    "this",
    "with",
    "us",
    "our",
    "any",
    "some",
    "so",
    # action words belong to the booking intents, not to FAQ topics
    "book",
    "appointment",
    "appointments",
    "schedule",
    "need",
    "want",
    "like",
    "see",
    "make",
    "visit",
    "hi",
    "hello",
    # conversational filler ("no" must not match the "no-show" keyword)
    "no",
    "not",
    "yes",
    "wait",
    "okay",
    "ok",
    "actually",
    "up",
}


def tokenize(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9]+", text.lower()):
        if t in STOP:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(t) > 4 and t.endswith(suf):
                t = t[: -len(suf)]
                break
        out.append(t)
    return out


@dataclass
class FaqHit:
    entry: FaqEntry
    score: float


class FaqIndex:
    def __init__(self, entries: list[FaqEntry], k1: float = 1.5, b: float = 0.75):
        self.entries = entries
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(f"{e.question} {e.question} {' '.join(e.keywords)}")) for e in entries]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / max(len(self.lens), 1)
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log((n - f + 0.5) / (f + 0.5) + 1) for t, f in df.items()}

    def search(self, query: str) -> FaqHit | None:
        q = [t for t in tokenize(query) if t in self.idf]
        if not q:
            return None
        best: FaqHit | None = None
        for e, d, ln in zip(self.entries, self.docs, self.lens, strict=True):
            score = 0.0
            for t in set(q):
                tf = d.get(t, 0)
                if tf:
                    score += self.idf[t] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * ln / self.avg))
            if best is None or score > best.score:
                best = FaqHit(e, score)
        return best if best and best.score > 0 else None
