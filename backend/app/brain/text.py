"""Text normalisation for speech-to-text output."""

from __future__ import annotations

import re

from app.textutil import strip_accents

_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ORD_UNITS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
    "thirteenth": 13,
    "fourteenth": 14,
    "fifteenth": 15,
    "sixteenth": 16,
    "seventeenth": 17,
    "eighteenth": 18,
    "nineteenth": 19,
}
_ORD_TENS = {"twentieth": 20, "thirtieth": 30}
_TIME_PREPS = {"at", "around", "about", "after", "before", "by", "to", "past", "until", "from", "of"}
_MONTHS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
}
_ONE_NEXT = {"pm", "am", "oclock", "o'clock", "thirty", "fifteen", "forty", "forty-five", "p.m.", "a.m."}


def ordinal_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def words_to_digits(text: str) -> str:
    """ "twenty five" -> "25", "march fifth" -> "march 5th", "two thousand five" -> "2005".

    The word "one" is only converted in time/date contexts so "that one" stays "that one".
    """
    toks = re.findall(r"[\w'.:/@-]+|[^\w\s]", text.lower())
    toks = [t for tok in toks for t in (tok.split("-") if re.fullmatch(r"[a-z]+-[a-z]+", tok) else [tok])]
    out: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prev = out[-1] if out else ""
        if t in _TENS:
            if nxt in _UNITS and 1 <= _UNITS[nxt] <= 9:
                out.append(str(_TENS[t] + _UNITS[nxt]))
                i += 2
                continue
            if nxt in _ORD_UNITS and _ORD_UNITS[nxt] <= 9:
                n = _TENS[t] + _ORD_UNITS[nxt]
                out.append(f"{n}{ordinal_suffix(n)}")
                i += 2
                continue
            out.append(str(_TENS[t]))
        elif t in _UNITS:
            n = _UNITS[t]
            if t == "one" and not (prev in _TIME_PREPS or prev in _MONTHS or nxt in _ONE_NEXT):
                out.append(t)
            elif nxt == "thousand":
                total = n * 1000
                i += 2
                if i < len(toks) and toks[i] == "and":
                    i += 1
                if i < len(toks) and toks[i] in _UNITS and 1 <= _UNITS[toks[i]] <= 9:
                    total += _UNITS[toks[i]]
                    i += 1
                elif i < len(toks) and toks[i] in _TENS:
                    total += _TENS[toks[i]]
                    i += 1
                    if i < len(toks) and toks[i] in _UNITS and 1 <= _UNITS[toks[i]] <= 9:
                        total += _UNITS[toks[i]]
                        i += 1
                out.append(str(total))
                continue
            else:
                out.append(str(n))
        elif t in _ORD_UNITS:
            n = _ORD_UNITS[t]
            out.append(f"{n}{ordinal_suffix(n)}")
        elif t in _ORD_TENS:
            n = _ORD_TENS[t]
            out.append(f"{n}{ordinal_suffix(n)}")
        else:
            out.append(t)
        i += 1
    return " ".join(out)


def normalize(text: str) -> str:
    """Lowercase, strip accents, expand spoken clock phrases and number words."""
    s = strip_accents(text).lower().replace("\u2019", "'").strip()
    s = re.sub(r"\b(a\.m\.|a\. m\.)", "am", s)
    s = re.sub(r"\b(p\.m\.|p\. m\.)", "pm", s)
    s = re.sub(r"\b(noon|midday)\b", "12 pm", s)
    s = re.sub(r"\b(no|nope|nah)\s*,", "\\1 \u00b7", s)  # "no, 3 pm" = correction, not "not 3 pm"
    s = re.sub(r"[,;!?]", " ", s)
    s = re.sub(r"\.(?=\s|$)", " ", s)
    s = words_to_digits(s)
    s = re.sub(r"\b(\d{1,2}) ([0-5]\d) (am|pm)\b", r"\1:\2 \3", s)
    s = re.sub(r"\b(at|around|about|after|before) (\d{1,2}) ([0-5]\d)\b", r"\1 \2:\3", s)
    s = re.sub(r"\bhalf past (\d{1,2})\b", r"\1:30", s)
    s = re.sub(r"\bquarter past (\d{1,2})\b", r"\1:15", s)
    s = re.sub(r"\b(?:quarter to|a quarter to) (\d{1,2})\b", lambda mo: f"{(int(mo.group(1)) - 2) % 12 + 1}:45", s)
    s = re.sub(r"(\d)\s*(am|pm)\b", r"\1 \2", s)
    return re.sub(r"\s+", " ", s).strip()


_YES = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "correct",
    "right",
    "absolutely",
    "definitely",
    "please",
    "affirmative",
    "fine",
    "perfect",
    "sounds",
    "good",
    "great",
    "confirm",
    "proceed",
}
_YES_PHRASES = (
    "that's right",
    "thats right",
    "that is right",
    "go ahead",
    "do it",
    "book it",
    "sounds good",
    "that works",
    "that's fine",
    "please do",
    "i do",
    "let's do it",
    "works for me",
    "that is correct",
    "that's correct",
    "yes please",
)
_NO = {"no", "nope", "nah", "negative", "wrong", "incorrect", "don't", "dont"}
_NO_PHRASES = (
    "not really",
    "no thanks",
    "no thank you",
    "that's wrong",
    "that is wrong",
    "not that",
    "not correct",
    "don't do",
    "do not",
    "not quite",
    "that's not",
)


def yes_no(norm: str) -> str | None:
    """Return "yes", "no" or None. Looks at the start of the utterance and key phrases."""
    if not norm:
        return None
    if any(p in norm for p in _NO_PHRASES):
        return "no"
    if any(p in norm for p in _YES_PHRASES):
        return "yes"
    words = re.findall(r"[a-z']+", norm)
    head = words[:3]
    has_no = any(w in _NO for w in head)
    has_yes = any(w in _YES for w in head)
    if has_no and not has_yes:
        return "no"
    if has_yes and not has_no:
        return "yes"
    return None
