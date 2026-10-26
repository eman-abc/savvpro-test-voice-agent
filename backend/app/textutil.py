"""Small text helpers shared by the service layer and the dialogue brain."""

import re
import unicodedata


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_name(s: str) -> str:
    """Lowercase, strip accents/punctuation: "José O'Connor-Smith" -> "jose oconnor smith"."""
    s = strip_accents(s).lower().replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def phonetic_word(w: str) -> str:
    """A small phonetic key so speech-to-text spellings meet: Gonzales/Gonzalez, Jon/John, Katherine/Catherine."""
    w = normalize_name(w).replace(" ", "")
    if not w:
        return ""
    for a, b in (("ph", "f"), ("ck", "k"), ("kn", "n"), ("wr", "r"), ("gh", ""), ("x", "ks"), ("q", "k"), ("z", "s")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eiy])", "s", w).replace("c", "k")
    first, rest = ("a" if w[0] in "aeiou" else w[0]), re.sub(r"[aeiouyhw]", "", w[1:])
    out = first + rest
    return re.sub(r"(.)\1+", r"\1", out)


def phonetic_key(name: str) -> str:
    return " ".join(phonetic_word(p) for p in normalize_name(name).split())
