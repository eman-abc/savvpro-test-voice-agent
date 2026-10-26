"""Weighted-pattern intent scoring.

score(intent) = strongest matching pattern + 0.3 * the rest. Confidence compares the best
intent with the runner-up: conf = top / (top + second + 1). Close contests become a
clarifying question instead of a guess ("reschedule or cancel?").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CANT = r"(?:can'?t|cannot|won'?t|unable to|not able to|couldn'?t) (?:make|attend|come|go|get|be there|do)"
NOUN = r"(?:appointment|appt|visit|booking)"

PATTERNS: dict[str, list[tuple[str, float]]] = {
    "book": [
        (
            rf"\b(?:book|schedule|make|set up|arrange|get|need|want|would like|i'?d like|looking for|like to)\b.{{0,40}}\b(?:{NOUN}|slot|consultation|checkup|check up|physical)\b",
            3.0,
        ),
        (r"\b(?:book|booking)\b", 3.0),
        (r"\bschedule\b(?! change)", 1.5),
        (r"\bsee (?:a |the |an )?(?:doctor|dr|physician|nurse|provider|gp)\b", 3.0),
        (r"\bnew appointment\b", 3.0),
        (rf"\b{NOUN}\b", 1.0),
        (
            r"\b(?:flu shot|vaccin\w+|immuni\w+|blood (?:test|work|draw)|bloodwork|physical|check ?up|annual|lab work)\b",
            2.0,
        ),
        (
            r"\b(?:i'?m|i am|feel|feeling|been) (?:sick|unwell|ill)\b|\bnot feeling well\b|\bi have (?:a )?(?:cold|cough|fever|rash|sore throat|pain)\b",
            2.0,
        ),
        (r"\b(?:first time|new patient)\b", 1.5),
    ],
    "reschedule": [
        (r"\bre-?schedul\w*\b", 4.0),
        (rf"\b(?:move|change|shift|push|postpone|switch|swap)\b.{{0,30}}\b{NOUN}\b", 4.0),
        (r"\b(?:different|another|other) (?:day|time|date|slot)\b", 1.5),
        (rf"\b{CANT}\b", 2.0),
    ],
    "cancel": [
        (r"\bcancel(?!lation)\w*\b", 4.0),
        (r"\b(?:call off|no longer need|don'?t need (?:the|my)|won'?t be coming|not coming)\b", 3.0),
        (rf"\b{CANT}\b", 2.0),
    ],
    "list": [
        (rf"\b(?:when|what|which|where)\b.{{0,25}}\b(?:my|the)\b.{{0,15}}\b{NOUN}s?\b", 4.0),
        (rf"\b(?:do i have|have i got|am i)\b.{{0,20}}\b(?:{NOUN}|booked|scheduled)\b", 4.0),
        (
            rf"\b(?:check|confirm|verify|look up|list|tell me|hear|read)\b.{{0,20}}\b(?:my )?(?:upcoming )?{NOUN}s?\b",
            3.5,
        ),
        (rf"\bmy (?:upcoming |next )?{NOUN}s?\b", 1.0),
    ],
    "faq": [
        (r"\b(?:policy|policies|fee|fees|charge|charges|how much|how early|how long|what happens if|penalty)\b", 3.0),
    ],
    "handoff": [
        (
            r"\b(?:speak|talk|connect|transfer|put me through|get|need|want)\b.{0,25}\b(?:human|person|someone|somebody|receptionist|operator|representative|staff|agent|manager)\b",
            4.0,
        ),
        (r"\b(?:speak|talk) (?:to|with) (?:a |the |an )?(?:nurse|doctor)\b", 4.0),
        (r"\b(?:real person|human being|live person|live agent|operator|representative|receptionist)\b", 3.5),
        (
            r"\b(?:this is (?:stupid|useless|ridiculous|terrible|awful)|you'?re (?:useless|not (?:helping|understanding|listening))|not working|doesn'?t work|you don'?t understand|waste of time|frustrat\w+)\b",
            3.5,
        ),
        (r"\bagent\b", 1.5),
    ],
    "waitlist": [
        (r"\b(?:wait ?list|waiting list|notify me|let me know if|put me on the list|cancellation list)\b", 4.0),
    ],
    "repeat": [
        (
            r"\b(?:repeat|say (?:that |it )?again|come again|pardon|what did you say|didn'?t (?:hear|catch|get) (?:that|you)|sorry what|one more time)\b",
            4.0,
        ),
    ],
    "startover": [
        (
            r"\b(?:start over|start again|begin again|never ?mind|forget (?:it|that)|scrap that|go back to the start)\b",
            4.5,
        ),
        (r"\bcancel (?:that|this)$", 5.0),
        (r"\breset\b", 3.0),
    ],
    "goodbye": [
        (r"\b(?:bye|goodbye|good bye|hang up|see you|talk later|have a good (?:day|one))\b", 4.0),
        (
            r"\b(?:that'?s (?:all|it)|that is all|nothing else|no thanks|no thank you|i'?m (?:all )?(?:done|good|set)|all set|nothing more)\b",
            3.0,
        ),
    ],
    "thanks": [(r"\b(?:thanks|thank you|thx|cheers|appreciate it)\b", 3.0)],
    "help": [(r"\b(?:help|what can you do|what do you do|options|menu)\b", 2.5)],
    "greeting": [(r"^(?:hi|hello|hey|howdy|good (?:morning|afternoon|evening))\b", 1.5)],
}
COMPILED = {k: [(re.compile(p), w) for p, w in v] for k, v in PATTERNS.items()}
QUESTION_START = re.compile(r"^(?:what|when|where|how|do you|does the|is there|are you|can you tell|which|who)\b")
STRONG_GOODBYE = re.compile(r"\b(?:bye|goodbye|good bye|hang up|see you|talk later)\b")

LABELS = {
    "book": "book an appointment",
    "reschedule": "reschedule one",
    "cancel": "cancel one",
    "list": "hear your upcoming appointments",
    "faq": "ask a question",
    "handoff": "speak to a person",
    "waitlist": "join the waitlist",
}


@dataclass
class IntentResult:
    scores: dict[str, float]
    ranked: list[tuple[str, float]]
    matched: dict[str, list[str]] = field(default_factory=dict)

    @property
    def top(self) -> tuple[str, float]:
        return self.ranked[0] if self.ranked else ("none", 0.0)

    @property
    def second(self) -> tuple[str, float]:
        return self.ranked[1] if len(self.ranked) > 1 else ("none", 0.0)

    @property
    def confidence(self) -> float:
        t, s = self.top[1], self.second[1]
        return round(t / (t + s + 1.0), 3) if t else 0.0


@dataclass
class Decision:
    kind: str  # act | clarify | none
    intent: str | None = None
    alt: str | None = None


ACTIONS = ("book", "reschedule", "cancel", "list", "handoff")
CANT_RE = re.compile(CANT)


def classify(norm: str, faq_score: float = 0.0) -> IntentResult:
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for intent, pats in COMPILED.items():
        # "can't make it to my appointment" must not look like "make an appointment"
        text = CANT_RE.sub(" ", norm) if intent == "book" else norm
        hits = [(w, m.group(0)) for p, w in pats if (m := p.search(text))]
        if hits:
            ws = sorted((w for w, _ in hits), reverse=True)
            scores[intent] = ws[0] + 0.3 * sum(ws[1:])
            matched[intent] = [h for _, h in hits]
    # specific beats generic: a clear cancel/reschedule/list request is not also "book"
    if "book" in scores and max((scores.get(k, 0.0) for k in ("cancel", "reschedule", "list")), default=0) >= 3.5:
        scores["book"] *= 0.35
    if faq_score > 0:
        question = bool(QUESTION_START.search(norm))
        base = min(4.0, faq_score) + (1.0 if question else 0.0)
        action_top = max((scores.get(k, 0.0) for k in ACTIONS), default=0.0)
        if action_top >= 3.0 and not question and "faq" not in scores:
            base *= 0.4  # "I want to cancel" is an action, not a question about the cancellation policy
        prev = scores.get("faq", 0.0)
        scores["faq"] = max(base, prev) + 0.3 * min(base, prev)
        matched.setdefault("faq", []).append("faq-retrieval")
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return IntentResult({k: round(v, 2) for k, v in scores.items()}, [(k, round(v, 2)) for k, v in ranked], matched)


def decide(res: IntentResult) -> Decision:
    (top, ts), (sec, ss) = res.top, res.second
    if top == "greeting" and ts >= 1.5 and ss < 1.5:
        return Decision("act", "greeting")
    if ts < 1.5:
        return Decision("none")
    if res.confidence >= 0.55:
        return Decision("act", top)
    if ss >= 0.6 * ts and sec in LABELS and top in LABELS:
        return Decision("clarify", top, sec)
    if ts >= 3.0:
        return Decision("act", top)
    return Decision("none")
