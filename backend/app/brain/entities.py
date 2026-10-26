"""Entity extraction from normalised utterances (see text.normalize).

Everything here is regular expressions + small rule tables + fuzzy string matching.
Corrections ("Tuesday, no wait, Thursday") are handled with two rules shared by all
extractors: skip mentions that are negated ("not Tuesday", "instead of Tuesday") and let the
LAST remaining mention win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from rapidfuzz import fuzz

from app.rules import ClinicRules, Provider, Service
from app.services.clinic import TimeWindow

MONTH_RE = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
MONTHS = {
    m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])
}
WEEKDAY_RE = r"(monday|tuesday|tues|wednesday|weds|thursday|thurs|friday|saturday|sunday)"
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
NEG_BEFORE = re.compile(
    r"(?:\bnot|\bno|\bexcept|instead of|rather than|other than|isn't|aren't|don't want|do not want)\s*(?:a |an |the |for )?$"
)


def _month(tok: str) -> int:
    return MONTHS[tok[:3]]


def _negated(text: str, start: int) -> bool:
    lo = max(0, start - 16)
    m = NEG_BEFORE.search(text[lo:start])
    if not m:
        return False
    # a bare leading "no" ("no 3 pm") is a reply particle, i.e. "no, 3 pm", not a negation of the value
    return not (lo + m.start() == 0 and re.match(r"no\b", m.group(0)))


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            chars[i] = " "
    return "".join(chars)


# ---------------------------------------------------------------------------- dates ---
@dataclass
class DateEntity:
    value: date | None  # None for week_of / earliest
    raw: str
    kind: str  # absolute | relative | weekday | earliest | week_of
    note: str = ""
    span: tuple[int, int] = (0, 0)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def resolve_weekday(today: date, wd: int, qualifier: str | None) -> tuple[date, str]:
    """ "this X" = on/after today; "X" = next occurrence after today; "next X" = in next calendar week."""
    delta = (wd - today.weekday()) % 7
    if qualifier in ("this", "coming"):
        return today + timedelta(days=delta), ""
    d = today + timedelta(days=delta or 7)
    if qualifier == "next" and _monday(d) == _monday(today):
        return d + timedelta(days=7), "'next' read as the following calendar week"
    return d, ""


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _yearless(today: date, mo: int, d: int) -> date | None:
    cand = _safe_date(today.year, mo, d)
    if cand and cand < today:
        cand = _safe_date(today.year + 1, mo, d)
    return cand


def parse_dates(norm: str, today: date, allow_bare_day: bool = False) -> list[DateEntity]:
    cands: list[DateEntity] = []
    taken: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(a < y and x < b for x, y in taken)

    def add(m: re.Match, value: date | None, kind: str, note: str = "") -> None:
        if overlaps(*m.span()):
            return
        if kind in ("absolute", "relative", "weekday") and value is None:
            return
        taken.append(m.span())
        cands.append(DateEntity(value, m.group(0).strip(), kind, note, m.span()))

    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", norm):
        add(m, _safe_date(int(m[1]), int(m[2]), int(m[3])), "absolute")
    for m in re.finditer(rf"\b{MONTH_RE}\.? (\d{{1,2}})(?:st|nd|rd|th)?(?: (\d{{4}}))?\b", norm):
        v = _safe_date(int(m[3]), _month(m[1]), int(m[2])) if m[3] else _yearless(today, _month(m[1]), int(m[2]))
        add(m, v, "absolute")
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?{MONTH_RE}\b(?: (\d{{4}}))?", norm):
        v = _safe_date(int(m[3]), _month(m[2]), int(m[1])) if m[3] else _yearless(today, _month(m[2]), int(m[1]))
        add(m, v, "absolute")
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", norm):
        a, b = int(m[1]), int(m[2])
        mo, d, note = (a, b, "") if a <= 12 else (b, a, "day/month order assumed")
        if m[3]:
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            add(m, _safe_date(y, mo, d), "absolute", note)
        else:
            add(m, _yearless(today, mo, d), "absolute", note)
    for m in re.finditer(r"\bday after tomorrow\b", norm):
        add(m, today + timedelta(days=2), "relative")
    for m in re.finditer(r"\btomorrow\b", norm):
        add(m, today + timedelta(days=1), "relative")
    for m in re.finditer(r"\b(today|tonight)\b", norm):
        add(m, today, "relative")
    for m in re.finditer(r"\bin (\d{1,2}) (day|days|week|weeks)\b", norm):
        n = int(m[1]) * (7 if m[2].startswith("week") else 1)
        add(m, today + timedelta(days=n), "relative")
    for m in re.finditer(r"\bnext week\b", norm):
        add(m, _monday(today) + timedelta(days=7), "week_of", "week of")
    for m in re.finditer(r"\bthis week\b", norm):
        add(m, today, "week_of", "week of")
    for m in re.finditer(rf"\b(?:(next|this|coming) )?{WEEKDAY_RE}\b", norm):
        d, note = resolve_weekday(today, WEEKDAYS[m[2][:3]], m[1])
        add(m, d, "weekday", note)
    for m in re.finditer(
        r"\b(earliest|soonest|first available|next available|as soon as possible|asap|first opening|any day|whenever)\b",
        norm,
    ):
        add(m, None, "earliest")
    if allow_bare_day:
        for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)\b", norm):
            n = int(m[1])
            for k in range(3):
                mo = (today.month - 1 + k) % 12 + 1
                y = today.year + (today.month - 1 + k) // 12
                cand = _safe_date(y, mo, n)
                if cand and cand >= today:
                    add(m, cand, "absolute", "day of month")
                    break

    cands = [c for c in cands if not _negated(norm, c.span[0])]
    cands.sort(key=lambda c: c.span[0])
    return cands


def parse_dob(norm: str, today: date) -> date | None:
    """Full date of birth (year required). Handles "march 4 1990", "4/3/1990", "19 84" (nineteen eighty four)."""
    s = re.sub(r"\b(19|20) (\d{2})\b", r"\1\2", norm)
    cand: date | None = None
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        cand = _safe_date(int(m[1]), int(m[2]), int(m[3]))
    if not cand:
        m = re.search(rf"\b{MONTH_RE}\.? (\d{{1,2}})(?:st|nd|rd|th)?,? (\d{{4}})\b", s)
        if m:
            cand = _safe_date(int(m[3]), _month(m[1]), int(m[2]))
    if not cand:
        m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)? (?:of )?{MONTH_RE}\.?,? (\d{{4}})\b", s)
        if m:
            cand = _safe_date(int(m[3]), _month(m[2]), int(m[1]))
    if not cand:
        m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", s)
        if m:
            y = int(m[3])
            if len(m[3]) == 2:
                y += 2000 if y <= today.year % 100 else 1900
            a, b = int(m[1]), int(m[2])
            cand = _safe_date(y, a, b) or _safe_date(y, b, a)
    if cand and cand.year >= 1900 and cand < today:
        return cand
    return None


def strip_dob(norm: str, today: date) -> str:
    """Remove a spoken date of birth so it is not mistaken for an appointment date."""
    s = re.sub(r"\b(19|20) (\d{2})\b", r"\1\2", norm)
    dob = parse_dob(s, today)
    if not dob:
        return norm
    pats = [
        r"\b\d{4}-\d{2}-\d{2}\b",
        rf"\b{MONTH_RE}\.? \d{{1,2}}(?:st|nd|rd|th)?,? \d{{4}}\b",
        rf"\b\d{{1,2}}(?:st|nd|rd|th)? (?:of )?{MONTH_RE}\.?,? \d{{4}}\b",
        r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b",
    ]
    for p in pats:
        s2 = re.sub(p, " ", s, count=1)
        if s2 != s:
            return re.sub(r"\b(born|birthday|birth date|date of birth|dob|on)\s*(?=\s|$)", " ", s2)
    return norm


def has_dob_without_year(norm: str) -> bool:
    return bool(
        re.search(rf"\b{MONTH_RE}\.? \d{{1,2}}(?:st|nd|rd|th)?\b", norm)
        or re.search(rf"\b\d{{1,2}}(?:st|nd|rd|th)? (?:of )?{MONTH_RE}\b", norm)
    )


# ---------------------------------------------------------------------------- times ---
@dataclass
class TimeEntity:
    window: TimeWindow
    raw: str
    kind: str  # exact | period | after | before | around | between
    inferred: bool = False
    span: tuple[int, int] = (0, 0)


PERIODS = {
    "morning": (time(8, 0), time(11, 59)),
    "afternoon": (time(12, 0), time(16, 59)),
    "evening": (time(17, 0), time(20, 0)),
}


def infer_clock(h: int, minute: int, meridiem: str | None) -> tuple[time | None, bool]:
    """Return (time, inferred). Bare hours use clinic hours: 7-11 -> AM, 12-6 -> PM."""
    if minute > 59 or h > 23:
        return None, False
    if meridiem:
        if not 1 <= h <= 12:
            return None, False
        h24 = h % 12 + (12 if meridiem == "pm" else 0)
        return time(h24, minute), False
    if h >= 13:
        return time(h, minute), False
    if h == 0:
        return None, False
    if h == 12:
        return time(12, minute), True
    return time(h + 12 if h <= 6 else h, minute), True


_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s?(am|pm)?"


def parse_times(norm: str, allow_bare: bool = False) -> list[TimeEntity]:
    out: list[TimeEntity] = []
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(span[0] < y and x < span[1] for x, y in taken)

    def add(ent: TimeEntity) -> None:
        taken.append(ent.span)
        out.append(ent)

    for m in re.finditer(rf"\bbetween {_CLOCK} and {_CLOCK}\b", norm):
        mer2 = m[6] or m[3]
        mer1 = m[3] or m[6]
        a, ia = infer_clock(int(m[1]), int(m[2] or 0), mer1)
        b, ib = infer_clock(int(m[4]), int(m[5] or 0), mer2)
        if a and b and a < b and free(m.span()):
            add(TimeEntity(TimeWindow(a, b), m[0], "between", ia or ib, m.span()))
    for m in re.finditer(rf"\b(after|before|around|about) {_CLOCK}\b", norm):
        if not free(m.span()):
            continue
        t, inferred = infer_clock(int(m[2]), int(m[3] or 0), m[4])
        if t is None:
            continue
        kw = m[1]
        if kw == "after":
            add(TimeEntity(TimeWindow(t, time(20, 0)), m[0], "after", inferred, m.span()))
        elif kw == "before":
            end = (datetime.combine(date.min, t) - timedelta(minutes=1)).time()
            add(TimeEntity(TimeWindow(time(6, 0), end), m[0], "before", inferred, m.span()))
        else:
            lo = time(max(t.hour - 1, 0), t.minute)
            hi = time(min(t.hour + 1, 23), t.minute)
            add(TimeEntity(TimeWindow(lo, hi, center=t), m[0], "around", inferred, m.span()))
    for m in re.finditer(r"\b(\d{1,2}):(\d{2})\s?(am|pm)?\b", norm):
        if free(m.span()):
            t, inferred = infer_clock(int(m[1]), int(m[2]), m[3])
            if t:
                add(TimeEntity(TimeWindow(t, t, exact=t), m[0], "exact", inferred, m.span()))
    for m in re.finditer(r"\b(\d{1,2})\s?(am|pm)\b", norm):
        if free(m.span()):
            t, inferred = infer_clock(int(m[1]), 0, m[2])
            if t:
                add(TimeEntity(TimeWindow(t, t, exact=t), m[0], "exact", inferred, m.span()))
    for m in re.finditer(r"\b(\d{1,2}) ?o'?clock\b", norm):
        if free(m.span()):
            t, inferred = infer_clock(int(m[1]), 0, None)
            if t:
                add(TimeEntity(TimeWindow(t, t, exact=t), m[0], "exact", True, m.span()))
    for m in re.finditer(r"\bat (\d{1,2})\b(?!:)", norm):
        if free(m.span()):
            t, inferred = infer_clock(int(m[1]), 0, None)
            if t:
                add(TimeEntity(TimeWindow(t, t, exact=t), m[0], "exact", True, m.span()))
    for m in re.finditer(r"(?<!good )\b(morning|afternoon|evening)\b", norm):
        if free(m.span()):
            lo, hi = PERIODS[m[1]]
            add(TimeEntity(TimeWindow(lo, hi), m[0], "period", False, m.span()))
    if allow_bare and not out:
        for m in re.finditer(r"(?<![\d/:-])\b(\d{1,2})\b(?![\d/:-])(?! ?(?:st|nd|rd|th|days?|weeks?|minutes?))", norm):
            t, inferred = infer_clock(int(m[1]), 0, None)
            if t:
                add(TimeEntity(TimeWindow(t, t, exact=t), m[0], "exact", True, m.span()))
    out = [e for e in out if not _negated(norm, e.span[0])]
    out.sort(key=lambda e: e.span[0])
    return out


@dataclass
class DateTimeEntities:
    dates: list[DateEntity]
    times: list[TimeEntity]


def extract_datetime(
    norm: str, today: date, allow_bare_day: bool = False, allow_bare_time: bool = False
) -> DateTimeEntities:
    """Dates first; their spans are masked so "march 5" is not mistaken for 5 o'clock."""
    dates = parse_dates(norm, today, allow_bare_day)
    masked = _mask(norm, [d.span for d in dates])
    return DateTimeEntities(dates, parse_times(masked, allow_bare_time))


# ----------------------------------------------------------------- choices / yes-no ---
def parse_choice(norm: str, n: int) -> int | None:
    """0-based index of the option chosen by "the second one", "option 2", "last", or "2"."""
    m = re.search(r"\b(\d)(?:st|nd|rd|th)\b", norm) or re.search(r"\b(?:option|number|choice|#) ?(\d)\b", norm)
    if m and 1 <= int(m[1]) <= n:
        return int(m[1]) - 1
    if re.search(r"\b(last|latest|final)\b", norm):
        return n - 1
    if re.fullmatch(r"(?:the |number |option )?(\d)(?: one)?", norm) and 1 <= int(re.search(r"\d", norm)[0]) <= n:  # type: ignore[index]
        return int(re.search(r"\d", norm)[0]) - 1  # type: ignore[index]
    return None


# --------------------------------------------------------------------------- people ---
NAME_STOP = {
    "my",
    "name",
    "is",
    "im",
    "i'm",
    "i",
    "am",
    "this",
    "its",
    "it's",
    "the",
    "and",
    "born",
    "on",
    "date",
    "of",
    "birth",
    "birthday",
    "dob",
    "was",
    "hi",
    "hello",
    "hey",
    "yes",
    "yeah",
    "please",
    "thanks",
    "thank",
    "you",
    "first",
    "last",
    "a",
    "an",
    "for",
    "to",
    "at",
    "in",
    "calling",
    "speaking",
    "with",
    "patient",
    "new",
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "mister",
    "call",
    "me",
    "uh",
    "um",
    "well",
    "so",
    "okay",
    "ok",
    "it",
    "that",
    "number",
    "phone",
    "book",
    "appointment",
    "schedule",
    "cancel",
    "reschedule",
    "want",
    "need",
    "like",
    "would",
}
NAME_BLOCK = {
    "sick",
    "fine",
    "good",
    "great",
    "here",
    "ready",
    "back",
    "not",
    "unwell",
    "looking",
    "trying",
    "having",
    "feeling",
    "sorry",
    "confused",
    "lost",
    "late",
    "nobody",
    "someone",
    "nothing",
    "something",
    "help",
    "human",
    "person",
    "operator",
    "again",
    "nope",
    "no",
    "yep",
    "sure",
    "correct",
    "wrong",
    "repeat",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "today",
    "tomorrow",
    "morning",
    "afternoon",
    "evening",
    "noon",
    "doctor",
    "nurse",
    "receptionist",
    "representative",
}
_MONTH_WORDS = set(MONTHS) | {
    "january",
    "february",
    "march",
    "april",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "sept",
}
_INTRO = re.compile(r"(?:my name is|my name's|name is|name's|this is|i am|i'm|its|it's|call me|speaking is|it is)\s+")


def extract_name(norm: str, expecting: bool = False) -> tuple[str | None, str | None]:
    """Return (first, last). With `expecting`, a bare "Maria Gonzalez" counts; otherwise an
    introduction like "my name is ..." is required. A lone token returns (token, None)."""
    s = re.sub(r"\b\d+(?:st|nd|rd|th)?\b|[/:.-]", " ", norm)
    s = re.sub(r"\b(dr|doctor)\b", "dr", s)
    intro = _INTRO.search(s)
    body = s[intro.end() :] if intro else s
    toks: list[str] = []
    for t in re.findall(r"[a-z']+", body):
        if t in NAME_STOP or t in _MONTH_WORDS:
            if toks:
                break
            continue
        if t in NAME_BLOCK:
            if toks:
                break
            return None, None
        toks.append(t)
    if not toks or len(toks) > 4:
        return None, None
    if not intro and not expecting:
        return None, None
    if not intro and expecting:
        rest = [t for t in re.findall(r"[a-z']+", s) if t not in NAME_STOP and t not in _MONTH_WORDS]
        if len(rest) != len(toks):  # extra words mixed in: not a plain name reply
            return None, None
    toks = [t.strip("'") for t in toks]
    if len(toks) == 1:
        return toks[0].title(), None
    return toks[0].title(), " ".join(t.title() for t in toks[1:])


def parse_phone(norm: str) -> str | None:
    digits = re.sub(r"\D", "", norm)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


# --------------------------------------------------------- services and providers ---
def match_service(norm: str, rules: ClinicRules) -> Service | None:
    """Longest keyword match wins; negated mentions are skipped; the last mention wins."""
    best: list[tuple[int, int, int, Service]] = []  # (position, length, order, service)
    for order, svc in enumerate(rules.services):
        for kw in [*svc.keywords, svc.name]:
            pat = r"\b" + re.escape(kw) + r"s?\b"
            for m in re.finditer(pat, norm):
                if not _negated(norm, m.start()):
                    best.append((m.start(), len(kw), order, svc))
            if " " not in kw and len(kw) >= 5:
                for m in re.finditer(r"[a-z]+", norm):
                    tok = m.group(0)
                    if len(tok) >= 5 and tok != kw and fuzz.ratio(tok, kw) >= 88 and not _negated(norm, m.start()):
                        best.append((m.start(), len(kw), order, svc))
    if not best:
        return None
    last_pos = max(b[0] for b in best)
    # among overlapping/near mentions prefer the longest keyword; across mentions prefer the last
    tail = [b for b in best if b[0] >= last_pos - 12]
    return max(tail, key=lambda b: (b[1], -b[2]))[3]


ANY_PROVIDER_STRONG = re.compile(
    r"\b(any (doctor|provider|one|of them|nurse)|anyone|anybody|whoever|no preference|no one in particular)\b"
)
ANY_PROVIDER_WEAK = re.compile(
    r"\b(any|either|whichever|don'?t care|doesn'?t matter|does not matter|first available|whoever is free|doesnt matter|dont care|no)\b"
)


def match_provider(norm: str, rules: ClinicRules, expecting: bool = False) -> str | None:
    """Return a provider key, "any", or None."""
    found: list[tuple[int, str]] = []
    for p in rules.providers:
        for name_tok in {t.lower() for t in p.name.split()}:
            if len(name_tok) < 3:
                continue
            for m in re.finditer(r"[a-z]+", norm):
                tok = m.group(0)
                ok = tok == name_tok or (len(tok) >= 5 and len(name_tok) >= 5 and fuzz.ratio(tok, name_tok) >= 85)
                if ok and not _negated(norm, m.start()):
                    found.append((m.start(), p.key))
    if found:
        return max(found)[1]
    if ANY_PROVIDER_STRONG.search(norm) or (expecting and ANY_PROVIDER_WEAK.search(norm)):
        return "any"
    return None


def provider_label(p: Provider) -> str:
    return p.spoken_name
