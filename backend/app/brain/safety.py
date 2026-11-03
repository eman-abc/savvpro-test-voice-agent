"""Emergency detection: deterministic, shared by the rules engine and the LLM agent (which runs it BEFORE the model sees the text).

A keyword matches in two ways:
1. as a phrase ("chest pain" in "I have chest pain");
2. by proximity, for multi-word keywords: every word of the keyword appears within a few words of the others, in any order
   ("pain in my chest", "my chest is tight").
It errs on the side of raising the alarm: a false alarm costs a repeated call, a missed emergency costs far more.
"""

from __future__ import annotations

import itertools
import re

WINDOW = 4  # max distance, in words, between the first and last word of a multi-word keyword


def emergency_match(text: str, keywords: list[str], window: int = WINDOW) -> str | None:
    """Return the first keyword found in `text` (lower-cased words), or None."""
    words = text.split()
    for kw in keywords:
        if re.search(r"\b" + re.escape(kw), text):
            return kw
        parts = kw.split()
        if len(parts) < 2:
            continue
        positions = [[i for i, w in enumerate(words) if w.startswith(p)] for p in parts]
        if all(positions) and min(max(c) - min(c) for c in itertools.product(*positions)) <= window:
            return kw
    return None
