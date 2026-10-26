"""Slot-filling dialogue state machine.

Every decision is made by code in this file plus the rules in config/clinic.yaml.

Top-level steps:   ROUTE -> IDENTIFY|REGISTER -> SELECT|COLLECT -> OFFER -> CONFIRM -> MORE -> ENDED
Inside COLLECT the brain fills a "frame" of slots (service, doctor, date, time) in whatever
order the caller supplies them, accepts corrections at any time, and asks only for what is missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from app.brain import messages_ur, urdu
from app.brain.entities import (
    DateEntity,
    extract_datetime,
    extract_name,
    has_dob_without_year,
    match_provider,
    match_service,
    parse_choice,
    parse_dob,
    parse_phone,
    strip_dob,
)
from app.brain.intents import LABELS, QUESTION_START, STRONG_GOODBYE, Decision, IntentResult, classify, decide
from app.brain.retrieval import FaqHit, FaqIndex
from app.brain.speech import a_an, cap, fmt_date, fmt_time, join_or, on_date, weekday_names
from app.brain.state import PROGRESS_STEPS, STEP_TO_PROGRESS, DialogState, Trace
from app.brain.text import normalize, yes_no
from app.models import Appointment, Patient
from app.rules import WEEKDAYS
from app.services.clinic import (
    BookingError,
    ClinicService,
    PatientConflictError,
    Slot,
    SlotTakenError,
    TimeWindow,
    spread_pick,
)
from app.textutil import phonetic_key

FLOWS = ("book", "reschedule", "cancel", "list")
SWITCH = {
    "book": ("cancel", "list"),
    "reschedule": ("cancel", "list"),
    "cancel": ("book", "reschedule", "list"),
    "list": ("book", "reschedule", "cancel"),
}
NEW_PATIENT_RE = re.compile(
    r"\b(new patient(?! (?:visit|appointment|consult))|first time|never been|i'?m new|haven'?t been|not a patient yet|register|sign up)\b"
)
ANYTIME_RE = re.compile(
    r"\b(any ?time|whenever|doesn'?t matter|does not matter|no preference|flexible|all day|anything works)\b"
)
MENU = "I can book, reschedule or cancel an appointment, check your upcoming visits, or answer questions. What would you like to do?"
HINTS = {
    "ROUTE": "You can say things like 'book an appointment', 'cancel my appointment', or 'what are your hours'.",
    "IDENTIFY": "For example: 'Maria Gonzalez, March 14th 1985'.",
    "REGISTER": "Please give me your full name, your date of birth with the year, and a 10-digit phone number.",
    "COLLECT": "For example: 'tomorrow at 3 PM' or 'Thursday morning'.",
    "OFFER": "You can say 'the first one', or give me a different day or time.",
    "CONFIRM": "Please say yes or no.",
    "SELECT": "You can say 'the first one', or give me the date.",
    "MORE": "Please say yes or no, or tell me what you need.",
    "WAITLIST": "Please say yes or no.",
    "CLARIFY": "Please tell me which of the two you would like.",
}
PAIR_TEXT = {
    frozenset({"reschedule", "cancel"}): "Would you like to reschedule your appointment, or cancel it?",
}


@dataclass
class TurnResult:
    reply: str
    trace: dict[str, Any]
    state: DialogState
    progress: dict[str, Any]
    ended: bool
    appointment: dict[str, Any] | None = None


class Brain:
    def __init__(self, svc: ClinicService, faq: FaqIndex, actor: str, lock_failures: int = 5, lock_minutes: int = 15):
        self.svc, self.rules, self.faq, self.actor = svc, svc.rules, faq, actor
        self.lock_failures, self.lock_minutes = lock_failures, lock_minutes
        self.st = DialogState()
        self.tr = Trace(0, "", "")
        self.today = date.today()
        self._missed = False
        self._appointment: dict[str, Any] | None = None

    # ================================================================= public API ===
    def start(self, state: DialogState) -> TurnResult:
        self.st, self.today = state, self.svc.now().date()
        self.tr = Trace(0, "", "")
        self.tr.step("note", "Call started", "Waiting in ROUTE for the caller's request")
        reply = self._(
            "Hello, and thank you for calling {0}. I'm the automated assistant. {1}", self.clinic_name, self._(MENU)
        )
        state.last_reply = reply
        return self._finish(reply, "ROUTE")

    def handle(self, state: DialogState, text: str) -> TurnResult:
        self.st = state
        self.today = self.svc.now().date()
        self._missed, self._appointment = False, None
        state.turn += 1
        prev = state.step
        text = (text or "").strip()[:500]
        spoken_urdu = urdu.is_urdu(text)
        if spoken_urdu and state.lang != "ur":
            state.lang = "ur"  # the caller is speaking Urdu (or Roman Urdu): answer in Urdu
        norm = normalize(urdu.to_canonical(text, state.ur_names) if spoken_urdu else text)
        self.tr = Trace(state.turn, text, norm)
        if spoken_urdu:
            self.tr.step("note", "Urdu understood", "mapped to the same canonical vocabulary the English rules use")
        if state.ended:
            reply = self._("This call has ended. Please start a new call.")
        elif not text:
            reply = self._silence()
        else:
            state.silences = 0
            reply = self._process(norm)
        self._remember_language()
        if not self._missed:
            state.misses = 0
        state.last_reply = reply
        return self._finish(reply, prev)

    # ============================================================== turn pipeline ===
    def _process(self, norm: str) -> str:
        st, tr = self.st, self.tr
        if not norm:
            tr.step("note", "No speech detected")
            return self._miss()
        for kw in self.rules.emergency_keywords:
            if re.search(r"\b" + re.escape(kw), norm):
                tr.step("rule", "Emergency keyword", f"matched '{kw}' - safety rule overrides everything else")
                return self._end(
                    "emergency",
                    self._(
                        "If this is a medical emergency, please hang up and call {0} now. I can't help with emergencies. Please stay safe. Goodbye.",
                        self.emergency_number,
                    ),
                )
        hit = self.faq.search(norm)
        res = classify(norm, hit.score if hit else 0.0)
        dec = decide(res)
        self._record_intent(res, dec, hit)
        if dec.kind == "act":
            i = dec.intent
            if i == "handoff":
                return self._handoff("caller asked for a person or showed frustration")
            if i == "repeat":
                tr.step("rule", "Repeat requested", "Replaying the last reply, state unchanged")
                return st.last_reply or self._(MENU)
            if i == "startover":
                return self._startover()
            if i == "goodbye" and (STRONG_GOODBYE.search(norm) or st.step in ("ROUTE", "MORE")):
                return self._goodbye()
            if i == "help":
                return self._(MENU) if st.step == "ROUTE" else f"{self._('Sure.')} {self._reprompt()}"
        return getattr(self, f"_step_{st.step.lower()}")(norm, dec, hit)

    def _record_intent(self, res: IntentResult, dec: Decision, hit: FaqHit | None) -> None:
        self.tr.data["intent"] = {
            "name": dec.intent or (res.top[0] if res.ranked else "none"),
            "decision": dec.kind,
            "confidence": res.confidence,
            "alternative": dec.alt,
            "ranked": [{"name": n, "score": s} for n, s in res.ranked[:4]],
            "faq_match": hit.entry.id if hit and "faq" in res.scores else None,
        }

    def _finish(self, reply: str, prev_step: str) -> TurnResult:
        st, tr = self.st, self.tr
        tr.data["reply"] = reply
        tr.data["state"] = {"from": prev_step, "to": st.step, "flow": st.flow}
        tr.data["slots"] = self._slot_view()
        progress = {"steps": PROGRESS_STEPS, "current": STEP_TO_PROGRESS.get(st.step, 0), "verified": st.verified}
        return TurnResult(reply, tr.data, st, progress, st.ended, self._appointment)

    # ================================================================= utilities ===
    # ---- language-aware helpers: every spoken name, date, time and sentence goes through these ----
    @property
    def lang(self) -> str:
        return self.st.lang

    def _(self, text: str, *args: object) -> str:
        """Translate a reply template (English is the source text; Urdu comes from messages_ur)."""
        if self.lang != "en" and text.strip():
            core = text.strip()
            lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()) :]
            text = lead + messages_ur.UR.get(core, core) + trail
        return text.format(*args) if args else text

    @property
    def clinic_name(self) -> str:
        c = self.rules.clinic
        return (c.name_ur or c.name) if self.lang == "ur" else c.name

    @property
    def emergency_number(self) -> str:
        c = self.rules.clinic
        return c.emergency_ur if self.lang == "ur" else c.emergency_en

    def pname(self, key: str) -> str:
        p = self.rules.provider(key)
        return p.spoken_name_ur if self.lang == "ur" else p.spoken_name

    def pfull(self, key: str) -> str:
        p = self.rules.provider(key)
        return p.full_name_ur if self.lang == "ur" else f"{p.title} {p.name}"

    def svc_name(self, key: str) -> str:
        s = self.rules.service(key)
        return (s.name_ur or s.name) if self.lang == "ur" else s.name

    def ftime(self, dt: datetime) -> str:
        return urdu.ur_time(dt) if self.lang == "ur" else fmt_time(dt)

    def fdate(self, d: date) -> str:
        return urdu.ur_date(d, self.today) if self.lang == "ur" else fmt_date(d, self.today)

    def _on(self, d: date) -> str:
        return urdu.ur_on_date(d, self.today) if self.lang == "ur" else on_date(d, self.today)

    def jor(self, items: list[str], conj: str = "or") -> str:
        if self.lang == "ur":
            return urdu.ur_join(items, "اور" if conj == "and" else "یا")
        return join_or(items, conj)

    def wdays(self, days: list[int]) -> str:
        return urdu.ur_weekdays(days) if self.lang == "ur" else weekday_names(days)

    def wd(self, d: date) -> str:
        return urdu.UR_WEEKDAYS[d.weekday()] if self.lang == "ur" else d.strftime("%A")

    def fdob(self, d: date) -> str:
        return urdu.ur_dob(d) if self.lang == "ur" else d.strftime("%b %d, %Y")

    def fdob_long(self, d: date) -> str:
        return urdu.ur_dob(d) if self.lang == "ur" else f"{d.strftime('%B')} {d.day}, {d.year}"

    def display_name(self, latin: str) -> str:
        """In Urdu replies, use the caller's own Urdu spelling of a name when we heard one."""
        if self.lang == "ur":
            if latin.lower() in self.st.ur_names:
                return self.st.ur_names[latin.lower()]
            for k, v in self.st.ur_names.items():
                if phonetic_key(k) == phonetic_key(latin):
                    return v
        return latin

    def ordinals(self) -> list[str]:
        return [self._(w) for w in ("first", "second", "third", "fourth", "fifth")]

    def a_an(self, phrase: str) -> str:
        return phrase if self.lang == "ur" else a_an(phrase)

    def _silence(self) -> str:
        """The caller said nothing (the page sends an empty turn after a pause). Prompt once, then hang up."""
        st = self.st
        st.silences += 1
        self.tr.step("note", "Silence", f"{st.silences}/2: asking if the caller is still there")
        if st.silences >= 2:
            return self._end(
                "silence", self._("I haven't heard from you, so I'll end the call. Please call back any time. Goodbye.")
            )
        return f"{self._('Are you still there?')} {self._reprompt()}"

    def _remember_language(self) -> None:
        """Replies and reminders go out in the language this patient used last."""
        st = self.st
        if st.patient_id:
            p = self.svc.db.get(Patient, st.patient_id)
            if p is not None and p.preferred_lang != st.lang:
                p.preferred_lang = st.lang
                self.svc.db.commit()

    # ------------------------------------------------------------------------ waitlist ---
    def _waitlist_pref(self) -> dict[str, Any] | None:
        """What the caller asked for, as a waitlist request (None if we don't know enough or the feature is off)."""
        st, s, pol = self.st, self.st.slots, self.rules.policy
        if not self.svc.waitlist or not st.patient_id or not s.get("service") or not s.get("date"):
            return None
        if s["date"] == "earliest":
            d_from, d_to, when = (
                self.today,
                self.today + timedelta(days=pol.search_horizon_days),
                self._("the earliest available time"),
            )
        else:
            d_from = d_to = date.fromisoformat(s["date"])
            when = self._on(d_from)
        t_from = t_to = None
        w = self._window()
        if w and w.exact:
            t_from = (datetime.combine(date.min, w.exact) - timedelta(hours=1)).time()
            t_to = (datetime.combine(date.min, w.exact) + timedelta(hours=1)).time()
        elif w and not (w.start <= time(6, 0) and w.end >= time(20, 0)):
            t_from, t_to = w.start, w.end
        prov = s.get("provider") if s.get("provider") not in (None, "any") else None
        return {
            "service": s["service"],
            "provider": prov,
            "from": d_from,
            "to": d_to,
            "t_from": t_from,
            "t_to": t_to,
            "text": self._("{0} {1}", self.svc_name(s["service"]), when),
        }

    def _offer_waitlist(self, lead: str = "", hint: str = "") -> str | None:
        pref = self._waitlist_pref()
        if pref is None:
            return None
        self.st.step, self.st.pending = "WAITLIST", {"kind": "waitlist"}
        self.tr.step(
            "rule", "Waitlist offered", "nothing fits the request; offering to message the caller if a slot opens"
        )
        if lead:
            return self._(
                "{0}{1} Would you like me to add you to the waitlist? I'll message you if a slot opens, or you can try a different day.",
                lead,
                hint,
            )
        return self._(
            "Would you like me to add you to the waitlist for {0}? I'll message you if a slot opens.", pref["text"]
        )

    def _step_waitlist(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        r = self._maybe_interrupt(norm, dec, hit)
        if r:
            return r
        st = self.st
        yn = yes_no(norm)
        if yn == "yes":
            pref = self._waitlist_pref()
            if pref is None:
                st.step = "COLLECT"
                return self._advance()
            entry = self.svc.waitlist.join(  # type: ignore[union-attr]
                st.patient_id or -1,
                pref["service"],
                pref["provider"],
                pref["from"],
                pref["to"],
                pref["t_from"],
                pref["t_to"],
                st.lang,
            )
            st.actions.append("waitlisted")
            st.step, st.pending = "MORE", {}
            st.slots.pop("waitlist", None)
            self.tr.action("join_waitlist", f"waitlist entry #{entry.id}")
            return self._(
                "You're on the waitlist for {0}. I'll message you the moment a slot opens. Is there anything else I can help with?",
                pref["text"],
            )
        if yn == "no":
            for k in ("waitlist", "date", "time"):
                st.slots.pop(k, None)
            st.options, st.pending, st.step = [], {}, "COLLECT"
            return self._("No problem. What day would you like to try instead?")
        return self._miss()

    def _end(self, reason: str, reply: str) -> str:
        self.st.ended, self.st.end_reason, self.st.step = True, reason, "ENDED"
        return reply

    def _goodbye(self) -> str:
        self.tr.step("rule", "Caller finished", "goodbye detected")
        return self._end("goodbye", self._("Thank you for calling {0}. Take care, goodbye!", self.clinic_name))

    def _handoff(self, reason: str) -> str:
        st = self.st
        parts = [
            f"reason: {reason}",
            f"verified patient: {'yes' if st.verified else 'no'}",
            f"request: {st.flow or 'none yet'} (stopped at {st.step})",
        ]
        if st.slots:
            parts.append(
                "collected: " + ", ".join(f"{v['label']}={v['value']}" for v in self._slot_view() if v["value"])
            )
        st.handoff_summary = "; ".join(parts)
        self.tr.step("rule", "Hand-off to a human", st.handoff_summary)
        self.tr.action("handoff", st.handoff_summary)
        return self._end(
            "handoff",
            self._(
                "Of course. I'm transferring you to a receptionist now and I'll pass along what we've covered so far. Please hold. In this demo the call would go to the front desk on {0}.",
                self.rules.clinic.phone,
            ),
        )

    def _startover(self) -> str:
        st = self.st
        self.tr.step("rule", "Start over", "Cleared the current request; the verified patient is kept")
        st.flow, st.slots, st.options, st.choices, st.pending, st.target_id = None, {}, [], [], {}, None
        st.step = "ROUTE"
        return self._("No problem, we'll start fresh. {0}", self._(MENU))

    def _miss(self) -> str:
        st = self.st
        st.misses += 1
        self._missed = True
        self.tr.step("warn", "Not understood", f"consecutive misses: {st.misses}/3")
        if st.misses >= 3:
            return self._handoff("could not understand the caller after 3 attempts")
        lead = (
            self._("Sorry, I didn't catch that.")
            if st.misses == 1
            else self._("I'm still having trouble understanding.")
        )
        hint = "" if st.step == "ROUTE" else self._(HINTS.get(st.step, ""))
        return f"{lead} {hint} {self._reprompt()}".replace("  ", " ")

    def _faq(self, hit: FaqHit) -> str:
        self.st.actions.append("faq")
        self.tr.step(
            "rule",
            "FAQ answered from rules",
            f"matched '{hit.entry.id}' (BM25 score {hit.score:.1f}); answer is pre-written",
        )
        return (hit.entry.answer_ur or hit.entry.answer) if self.lang == "ur" else hit.entry.answer

    def _maybe_interrupt(self, norm: str, dec: Decision, hit: FaqHit | None) -> str | None:
        """Handle a mid-task question or a change of mind without losing progress."""
        st = self.st
        if dec.kind != "act":
            return None
        i = dec.intent
        if i == "faq" and hit and QUESTION_START.search(norm):
            dt = extract_datetime(norm, self.today)
            if not dt.dates and not dt.times:
                self.tr.step("switch", "Question during a task", "answering it, then returning to the same step")
                return self._("{0} Now, back to where we were. {1}", self._faq(hit), self._reprompt())
        if i == "waitlist" and st.flow == "book":
            offered = self._offer_waitlist()
            if offered:
                return offered
        if st.flow and i in SWITCH.get(st.flow, ()):
            return self._start_flow(i, norm)
        return None

    # ================================================================ slot view ===
    def _fmt_window(self, w: dict[str, Any]) -> str:
        return w.get("label", "")

    def _slot_view(self) -> list[dict[str, Any]]:
        st, s = self.st, self.st.slots
        if st.step == "IDENTIFY":
            v = st.verify
            name = " ".join(x for x in (v["first"], v["last"]) if x) or None
            dob = self.fdob(date.fromisoformat(v["dob"])) if v["dob"] else None
            return [{"label": self._("Name"), "value": name}, {"label": self._("Date of birth"), "value": dob}]
        if st.step == "REGISTER":
            r = st.register
            name = " ".join(x for x in (r["first"], r["last"]) if x) or None
            dob = self.fdob(date.fromisoformat(r["dob"])) if r["dob"] else None
            return [
                {"label": self._("Name"), "value": name},
                {"label": self._("Date of birth"), "value": dob},
                {
                    "label": self._("Phone"),
                    "value": (self._("on file") if r["phone"] else (self._("declined") if r["phone"] == "" else None)),
                },
            ]
        if st.flow in ("book", "reschedule") and st.step in ("COLLECT", "OFFER", "CONFIRM", "MORE", "WAITLIST"):
            svc = self.svc_name(s["service"]) if s.get("service") else None
            prov = None
            if s.get("provider") == "any":
                prov = self._("Any")
            elif s.get("provider"):
                prov = self.pname(s["provider"])
            d = s.get("date")
            dval = (
                self._("Earliest available") if d == "earliest" else (self.fdate(date.fromisoformat(d)) if d else None)
            )
            tval = s["time"]["label"] if s.get("time") else None
            return [
                {"label": self._("Service"), "value": svc},
                {"label": self._("Doctor"), "value": prov},
                {"label": self._("Date"), "value": dval},
                {"label": self._("Time"), "value": tval},
            ]
        if st.step in ("SELECT", "CONFIRM") and st.flow in ("cancel", "reschedule", "list") and st.target_id:
            a = self.svc.get_appointment(st.target_id)
            return [{"label": self._("Appointment"), "value": self._describe(a) if a else None}]
        return []

    # ================================================================== reprompts ===
    def _reprompt(self) -> str:
        st = self.st
        step = st.step
        if step == "IDENTIFY":
            return self._identify_prompt()
        if step == "REGISTER":
            return self._register_prompt()
        if step == "COLLECT":
            missing = self._missing_slots()
            return self._ask(missing[0]) if missing else self._advance()
        if step == "OFFER":
            return self._read_options(st.options)
        if step == "CONFIRM":
            return self._confirm_prompt()
        if step == "SELECT":
            return self._select_prompt()
        if step == "CLARIFY":
            return self._clarify_prompt()
        if step == "WAITLIST":
            pref = self._waitlist_pref()
            return self._(
                "Would you like me to add you to the waitlist for {0}? I'll message you if a slot opens.",
                pref["text"] if pref else "",
            )
        if step == "MORE":
            return self._("Is there anything else I can help you with?")
        return self._(MENU)

    # ================================================================== ROUTE ===
    def _step_route(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        if dec.kind == "clarify":
            return self._clarify(dec)
        i = dec.intent if dec.kind == "act" else None
        if i == "waitlist":
            return self._start_flow("book", norm, waitlist=True)
        if i in FLOWS:
            return self._start_flow(i, norm)
        if i == "faq" and hit:
            ans = self._faq(hit)
            self.st.step = "MORE"
            return self._("{0} Is there anything else I can help you with?", ans)
        if i == "greeting":
            return self._("Hello! {0}", self._(MENU))
        if i == "thanks":
            return self._("You're welcome! Is there anything else I can help with?")
        return self._miss()

    def _clarify(self, dec: Decision) -> str:
        a, b = dec.intent, dec.alt
        self.st.step = "CLARIFY"
        self.st.pending = {"kind": "clarify", "options": [a, b]}
        self.tr.step("rule", "Ambiguous request", f"'{a}' and '{b}' scored too close; asking instead of guessing")
        return self._clarify_prompt()

    def _clarify_prompt(self) -> str:
        a, b = self.st.pending.get("options", ["book", "cancel"])
        pair = PAIR_TEXT.get(frozenset({a, b}))
        if pair:
            return self._(pair)
        return self._("Would you like to {0}, or {1}?", self._(LABELS[a]), self._(LABELS[b]))

    def _step_clarify(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        opts = self.st.pending.get("options", [])
        if dec.kind == "act" and dec.intent in opts:
            return self._start_flow(dec.intent, norm)
        idx = parse_choice(norm, len(opts))
        if idx is not None:
            return self._start_flow(opts[idx], norm)
        return self._miss()

    def _start_flow(self, intent: str, norm: str, waitlist: bool = False) -> str:
        st, tr = self.st, self.tr
        if st.flow and st.flow != intent:
            tr.step("switch", "Request changed", f"{st.flow} -> {intent}; progress on the old request is dropped")
        st.flow, st.slots, st.options, st.choices, st.pending, st.target_id = intent, {}, [], [], {}, None
        if waitlist:
            st.slots["waitlist"] = True  # the caller asked for the waitlist: collect the details first
        tr.step("rule", f"Flow started: {intent}", "needs a verified patient" if intent != "faq" else "")
        if intent in ("book", "reschedule"):
            self._absorb(norm)
        if intent == "cancel":
            dts = extract_datetime(norm, self.today)
            if dts.dates and dts.dates[-1].value:
                st.pending["hint_date"] = dts.dates[-1].value.isoformat()
        first, last = extract_name(norm)
        dob = parse_dob(norm, self.today)
        if st.patient_id:
            return self._after_identity("")
        if first and last:
            st.verify["first"], st.verify["last"] = first, last
            tr.entity("name", f"{first} {last}", "volunteered up front")
        if dob:
            st.verify["dob"] = dob.isoformat()
            tr.entity("date_of_birth", dob.isoformat(), "volunteered up front")
        if NEW_PATIENT_RE.search(norm):
            tr.step("rule", "New patient", "caller says they are new; registering instead of verifying")
            st.step = "REGISTER"
            return self._("Welcome! I'll get you registered first. {0}", self._register_prompt())
        st.step = "IDENTIFY"
        return self._identify_progress(intro=True)

    # ================================================================ IDENTIFY ===
    def _identify_prompt(self) -> str:
        v = self.st.verify
        has_name, has_dob = bool(v["first"] and v["last"]), bool(v["dob"])
        if v["first"] and not v["last"]:
            return self._("Thanks, {0}. And your last name?", v["first"])
        if has_name and not has_dob:
            return self._("Thanks, {0}. And your date of birth, including the year?", v["first"])
        if has_dob and not has_name:
            return self._("Thank you. And your full name?")
        return self._(
            "To protect your privacy I need to verify who you are. Please tell me your full name and date of birth."
        )

    def _identify_progress(self, intro: bool = False) -> str:
        v = self.st.verify
        if v["first"] and v["last"] and v["dob"]:
            return self._lookup_identity()
        prefix = self._("I can help with that. ") if intro else ""
        extra = (
            self._(" If you're new to the clinic, just say 'new patient'.")
            if intro and not (v["first"] or v["dob"])
            else ""
        )
        return prefix + self._identify_prompt() + extra

    def _step_identify(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        r = self._maybe_interrupt(norm, dec, hit)
        if r:
            return r
        st, tr, v = self.st, self.tr, self.st.verify
        if NEW_PATIENT_RE.search(norm):
            tr.step("rule", "New patient", "switching from verification to registration")
            st.step = "REGISTER"
            return self._("No problem, I'll register you. {0}", self._register_prompt())
        expecting_name = not (v["first"] and v["last"])
        first, last = extract_name(norm, expecting=expecting_name)
        dob = parse_dob(norm, self.today)
        understood = False
        if first and last:
            v["first"], v["last"], understood = first, last, True
            tr.entity("name", f"{first} {last}")
        elif first and not v["first"]:
            v["first"], understood = first, True
            tr.entity("first_name", first)
        elif first and v["first"] and not v["last"]:
            v["last"], understood = first, True
            tr.entity("last_name", first)
        if dob:
            v["dob"], understood = dob.isoformat(), True
            tr.entity("date_of_birth", dob.isoformat())
        elif has_dob_without_year(norm):
            return self._("Thank you. I also need the year you were born.")
        if not understood:
            return self._miss()
        return self._identify_progress()

    def _lookup_identity(self) -> str:
        st, tr, v = self.st, self.tr, self.st.verify
        full = f"{v['first']} {v['last']}"
        pol = self.rules.policy
        if self.svc.verification_locked(full):
            tr.step("rule", "Verification locked", "too many failed checks for this name; sending to staff")
            return self._handoff("identity checks for this name are temporarily locked")
        patient, score = self.svc.find_patient(full, date.fromisoformat(v["dob"]))
        tr.step("check", "Identity check", f"date of birth must match exactly; name similarity {score}/100 (needs 82+)")
        if patient:
            self.svc.clear_verification_failures(full)
            st.patient_id, st.patient_first, st.verified = patient.id, patient.first_name, True
            st.established = self.svc.is_established(patient.id)
            tr.step(
                "rule",
                "Patient verified",
                "status: " + ("established patient" if st.established else "new patient (no past visits)"),
            )
            return self._after_identity(self._("Thank you, {0}. ", self.display_name(patient.first_name)))
        v["attempts"] += 1
        attempts = v["attempts"]
        heard = self._(
            "{0} {1}, born {2}",
            self.display_name(v["first"]),
            self.display_name(v["last"]),
            self.fdob_long(date.fromisoformat(v["dob"])),
        )
        v["first"] = v["last"] = v["dob"] = None
        tr.step(
            "warn",
            "Identity not matched",
            f"attempt {attempts}/{pol.max_verify_attempts}; the reply says what was heard, never which part failed",
        )
        settings_locked = self.svc.record_verification_failure(full, self.lock_failures, self.lock_minutes)
        if settings_locked:
            tr.step(
                "rule", "Name locked", f"{self.lock_failures} failed checks; locked for {self.lock_minutes} minutes"
            )
            return self._handoff("too many failed identity checks for this name")
        if attempts >= pol.max_verify_attempts:
            return self._handoff("identity could not be verified after repeated attempts")
        if attempts == 2:
            return self._(
                "I heard {0}, but still couldn't match those details. If you're new to the clinic, say 'new patient' and I'll register you. Otherwise, please say your full name and date of birth once more.",
                heard,
            )
        return self._(
            "I heard {0}, but I couldn't match those details. Please say your full name and date of birth again.", heard
        )

    def _after_identity(self, prefix: str) -> str:
        st = self.st
        if st.flow is None:
            st.step = "ROUTE"
            return self._("{0}What can I help you with today?", prefix)
        if st.flow == "list":
            return prefix + self._do_list()
        if st.flow in ("cancel", "reschedule"):
            return prefix + self._select_appointment()
        msg = self._validate_slots()
        return prefix + (msg + " " if msg else "") + self._advance(ack=True)

    # ================================================================= REGISTER ===
    def _register_prompt(self) -> str:
        r = self.st.register
        if not (r["first"] and r["last"]):
            return (
                self._("What is your full name?")
                if not r["first"]
                else self._("Thanks, {0}. And your last name?", r["first"])
            )
        if not r["dob"]:
            return self._("What is your date of birth, including the year?")
        if r["phone"] is None:
            return self._("And a 10-digit phone number for appointment reminders? You can also say 'skip'.")
        return self._("Thank you.")

    def _step_register(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        r0 = self._maybe_interrupt(norm, dec, hit)
        if r0:
            return r0
        st, tr, r = self.st, self.tr, self.st.register
        understood = False
        first, last = extract_name(norm, expecting=not (r["first"] and r["last"]))
        if first and last:
            r["first"], r["last"], understood = first, last, True
            tr.entity("name", f"{first} {last}")
        elif first and not r["first"]:
            r["first"], understood = first, True
        elif first and r["first"] and not r["last"]:
            r["last"], understood = first, True
        dob = parse_dob(norm, self.today)
        if dob:
            if (self.today - dob).days > 365 * 120:
                return self._("That date of birth doesn't look right. Could you say it again with the year?")
            r["dob"], understood = dob.isoformat(), True
            tr.entity("date_of_birth", dob.isoformat())
        elif has_dob_without_year(norm):
            return self._("Thank you. I also need the year you were born.")
        phone = parse_phone(norm)
        if phone:
            r["phone"], understood = phone, True
            tr.entity("phone", "***-***-" + phone[-4:], "masked")
        elif (
            r["first"]
            and r["last"]
            and r["dob"]
            and re.search(r"\b(skip|rather not|don'?t have|no phone|prefer not)\b", norm)
        ):
            r["phone"], understood = "", True
            tr.step("note", "Phone declined", "optional field skipped")
        if not understood:
            return self._miss()
        if not (r["first"] and r["last"] and r["dob"] and r["phone"] is not None):
            return self._register_prompt()
        dob_d = date.fromisoformat(r["dob"])
        existing, score = self.svc.find_patient(f"{r['first']} {r['last']}", dob_d)
        if existing:
            tr.step(
                "rule", "Already registered", f"name+DOB match an existing record ({score}/100); treating as verified"
            )
            patient = existing
        else:
            patient = self.svc.create_patient(r["first"], r["last"], dob_d, r["phone"] or None)
            tr.step("rule", "Patient registered", "new record created; counts as a new patient until their first visit")
            tr.action("register_patient", f"patient #{patient.id}")
        st.patient_id, st.patient_first, st.verified = patient.id, patient.first_name, True
        st.established = self.svc.is_established(patient.id)
        return self._after_identity(self._("Welcome, {0}, you're all set. ", self.display_name(patient.first_name)))

    # ================================================================ appointments ===
    def _describe(self, a: Appointment) -> str:
        p = self.rules.provider(a.provider.key)
        return self._(
            "{0} with {1} {2} at {3}",
            self.svc_name(a.service_key),
            self.pname(p.key),
            self._on(a.start_at.date()),
            self.ftime(a.start_at),
        )

    def _do_list(self) -> str:
        st = self.st
        appts = self.svc.upcoming(st.patient_id or -1)
        self.tr.step("check", "Looked up upcoming appointments", f"{len(appts)} found")
        st.actions.append("listed")
        st.step = "MORE"
        if not appts:
            st.pending = {"offer": "book"}
            return self._("You don't have any upcoming appointments. Would you like to book one?")
        items = [self._describe(a) for a in appts[:5]]
        lead = (
            self._("You have one upcoming appointment: ")
            if len(items) == 1
            else self._("You have {0} upcoming appointments: ", len(appts))
        )
        return lead + "; ".join(items) + self._(". Is there anything else I can help with?")

    def _select_appointment(self) -> str:
        st, tr = self.st, self.tr
        appts = self.svc.upcoming(st.patient_id or -1)
        tr.step("check", "Looked up upcoming appointments", f"{len(appts)} found")
        if not appts:
            st.step, st.pending = "MORE", {"offer": "book"}
            return self._("I don't see any upcoming appointments for you. Would you like to book one?")
        hint = st.pending.get("hint_date")
        if hint:
            same = [a for a in appts if a.start_at.date().isoformat() == hint]
            if len(same) == 1:
                tr.step("rule", "Matched appointment by date", f"caller mentioned {hint}")
                return self._picked(same[0])
        if len(appts) == 1:
            return self._picked(appts[0])
        st.choices = [a.id for a in appts[:5]]
        st.step = "SELECT"
        return self._select_prompt()

    def _select_prompt(self) -> str:
        st = self.st
        verb = self._("cancel" if st.flow == "cancel" else "reschedule")
        ords = self.ordinals()
        items = []
        for n, aid in enumerate(st.choices):
            a = self.svc.get_appointment(aid)
            if a:
                items.append(self._("The {0} is {1}.", ords[n], self._describe(a)))
        return self._(
            "You have {0} upcoming appointments. {1} Which one would you like to {2}?",
            len(items),
            " ".join(items),
            verb,
        )

    def _step_select(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        r = self._maybe_interrupt(norm, dec, hit)
        if r:
            return r
        st = self.st
        appts = [a for a in (self.svc.get_appointment(i) for i in st.choices) if a]
        idx = parse_choice(norm, len(appts))
        if idx is None:
            dts = extract_datetime(norm, self.today, allow_bare_day=True)
            if dts.dates and dts.dates[-1].value:
                same = [i for i, a in enumerate(appts) if a.start_at.date() == dts.dates[-1].value]
                if len(same) == 1:
                    idx = same[0]
        if idx is None:
            prov = match_provider(norm, self.rules)
            svc = match_service(norm, self.rules)
            cand = [
                i
                for i, a in enumerate(appts)
                if (not prov or prov == "any" or a.provider.key == prov) and (not svc or a.service_key == svc.key)
            ]
            if (prov or svc) and len(cand) == 1:
                idx = cand[0]
        if idx is None:
            if yes_no(norm) == "no":
                st.step = "MORE"
                return self._("Okay, I've left everything as it is. Is there anything else I can help with?")
            return self._miss()
        self.tr.step("rule", "Appointment selected", f"choice {idx + 1} of {len(appts)}")
        return self._picked(appts[idx])

    def _picked(self, a: Appointment) -> str:
        st, tr = self.st, self.tr
        st.target_id = a.id
        late = self.svc.is_late_change(a)
        hours = self.rules.policy.cancel_notice_hours
        desc = self._describe(a)
        tr.step(
            "check",
            "Notice-period rule",
            f"starts in {(a.start_at - self.svc.now()).total_seconds() / 3600:.1f}h; late if under {hours}h -> {'LATE' if late else 'ok'}",
        )
        if st.flow == "cancel":
            st.step, st.pending = "CONFIRM", {"kind": "cancel", "late": late}
            warn = (
                self._(
                    "That is within {0} hours, so cancelling now counts as a late cancellation and a fee may apply. ",
                    hours,
                )
                if late
                else ""
            )
            return self._("I found your {0}. {1}Do you want to cancel it?", desc, warn)
        st.slots.update({"service": a.service_key, "provider": a.provider.key, "provider_asked": True})
        if late:
            st.step, st.pending = "CONFIRM", {"kind": "late_continue"}
            return self._(
                "I found your {0}. That is within {1} hours, so changing it counts as a late cancellation and a fee may apply. Do you still want to reschedule?",
                desc,
                hours,
            )
        return self._("I found your {0}. ", desc) + self._advance()

    # ===================================================================== MORE ===
    def _step_more(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        st = self.st
        if dec.kind == "clarify":
            return self._clarify(dec)
        if dec.kind == "act":
            if dec.intent in FLOWS:
                st.pending = {}
                return self._start_flow(dec.intent, norm)
            if dec.intent == "faq" and hit:
                return self._("{0} Is there anything else I can help with?", self._faq(hit))
            if dec.intent == "thanks":
                return self._goodbye()
        yn = yes_no(norm)
        if yn == "yes":
            offer = st.pending.get("offer")
            st.pending = {}
            if offer == "book":
                return self._start_flow("book", "")
            st.step = "ROUTE"
            return self._("Sure. {0}", self._(MENU))
        if yn == "no":
            return self._goodbye()
        return self._miss()

    # ================================================================ slot filling ===
    def _missing_slots(self) -> list[str]:
        st, s = self.st, self.st.slots
        missing: list[str] = []
        if st.flow == "book" and not s.get("service"):
            missing.append("service")
        if st.flow == "book" and s.get("service") and not s.get("provider") and not s.get("provider_asked"):
            if len(self.rules.providers_for(s["service"])) > 1:
                missing.append("provider")
            else:
                s["provider"] = "any"
        if not s.get("date"):
            missing.append("date")
        if s.get("date") != "earliest" and not s.get("time"):
            missing.append("time")
        return missing

    def _eligible_services(self) -> list[str]:
        est = bool(self.st.established)
        names = []
        for sv in self.rules.services:
            if (sv.requires == "new" and est) or (sv.requires == "established" and not est):
                continue
            names.append(self.svc_name(sv.key))
        return names

    def _ask(self, slot: str) -> str:
        s = self.st.slots
        if slot == "service":
            return self._("What kind of visit do you need? For example {0}.", self.jor(self._eligible_services()[:5]))
        if slot == "provider":
            names = self.jor([self.pname(p.key) for p in self.rules.providers_for(s["service"])])
            return self._(
                "Do you prefer a particular provider? {0} can do this visit. Or say 'any' for the first available.",
                names,
            )
        if slot == "date":
            if s.get("week_of"):
                return self._("Which day next week works for you?")
            return self._("What day would you like? You can also say 'earliest available'.")
        d = date.fromisoformat(s["date"])
        return self._("What time works {0}? You can say a time, or morning or afternoon.", self._on(d))

    def _advance(self, ack: bool = False) -> str:
        st, s = self.st, self.st.slots
        missing = self._missing_slots()
        if not missing and s.get("waitlist"):
            offered = self._offer_waitlist()
            if offered:
                return offered
            s.pop("waitlist", None)
        if missing:
            st.step = "COLLECT"
            lead = ""
            if ack and s.get("service") and st.flow == "book":
                lead = self._("I'll set up {0}. ", self.a_an(self.svc_name(s["service"])))
            return lead + self._ask(missing[0])
        return self._compute_options()

    def _label_window(self, ent_kind: str, w: TimeWindow, raw: str) -> str:
        if w.exact:
            return self.ftime(datetime.combine(date.min, w.exact))
        if ent_kind == "period":
            return raw
        if ent_kind == "after":
            return self._("after {0}", self.ftime(datetime.combine(date.min, w.start)))
        if ent_kind == "before":
            return self._(
                "before {0}",
                self.ftime(
                    datetime.combine(date.min, (datetime.combine(date.min, w.end) + timedelta(minutes=1)).time())
                ),
            )
        if ent_kind == "around" and w.center:
            return self._("around {0}", self.ftime(datetime.combine(date.min, w.center)))
        return self._(
            "{0} to {1}", self.ftime(datetime.combine(date.min, w.start)), self.ftime(datetime.combine(date.min, w.end))
        )

    def _show(self, slot: str, v: Any) -> str:
        if v is None:
            return "none"
        if slot == "service":
            return self.svc_name(v)
        if slot == "provider":
            return "any provider" if v == "any" else self.pname(v)
        if slot == "date":
            return "earliest available" if v == "earliest" else self.fdate(date.fromisoformat(v))
        if slot == "time":
            return v["label"]
        return str(v)

    def _absorb(self, norm: str, bare_time: bool = False, bare_day: bool = False) -> list[str]:
        """Pull slot values out of an utterance. Returns the names of slots that changed.
        A changed slot that already had a value is logged as a correction."""
        st, tr, s = self.st, self.tr, self.st.slots
        norm = strip_dob(norm, self.today)  # "born May 17 1995" is not an appointment date
        nxt = (self._missing_slots() or [None])[0]
        changed: list[str] = []

        def put(slot: str, new: Any, label: str) -> None:
            old = s.get(slot)
            if old == new:
                return
            s[slot] = new
            changed.append(slot)
            if old is not None:
                tr.step("correction", f"{label} corrected", f"{self._show(slot, old)} -> {self._show(slot, new)}")

        if st.flow == "book":
            sv = match_service(norm, self.rules)
            if sv:
                tr.entity("service", self.svc_name(sv.key))
                put("service", sv.key, "Service")
        if st.flow in ("book", "reschedule"):
            prov = match_provider(norm, self.rules, expecting=(nxt == "provider"))
            if prov:
                tr.entity("provider", "any" if prov == "any" else self.pname(prov))
                s["provider_asked"] = True
                put("provider", prov, "Doctor")
        dt = extract_datetime(
            norm, self.today, allow_bare_day=bare_day or nxt == "date", allow_bare_time=bare_time or nxt == "time"
        )
        picked: DateEntity | None = dt.dates[-1] if dt.dates else None
        weekday_ent = next((d for d in reversed(dt.dates) if d.kind == "weekday"), None)
        week_ent = next((d for d in reversed(dt.dates) if d.kind == "week_of"), None)
        if week_ent and weekday_ent and week_ent.value and weekday_ent.value:
            base = week_ent.value - timedelta(days=week_ent.value.weekday())
            picked = DateEntity(
                base + timedelta(days=weekday_ent.value.weekday()), "next week " + weekday_ent.raw, "weekday"
            )
        if picked:
            if picked.kind == "earliest":
                tr.entity("date", "earliest available", picked.raw)
                put("date", "earliest", "Date")
            elif picked.kind == "week_of" and picked.value:
                tr.entity("week", picked.value.isoformat(), picked.raw)
                s["week_of"] = picked.value.isoformat()
                if s.get("date") not in (None, "earliest"):
                    pass
                else:
                    s.pop("date", None)
            elif picked.value:
                tr.entity("date", picked.value.isoformat(), picked.raw)
                if picked.note:
                    tr.step("note", "Date interpretation", picked.note)
                s.pop("week_of", None)
                put("date", picked.value.isoformat(), "Date")
        if dt.times:
            te = dt.times[-1]
            label = self._label_window(te.kind, te.window, te.raw)
            tr.entity("time", label, te.raw)
            if te.inferred and te.window.exact:
                tr.step("note", "AM/PM assumed", f"'{te.raw}' read as {label} because the clinic is open 8 AM to 6 PM")
            payload = {
                "start": te.window.start.isoformat(),
                "end": te.window.end.isoformat(),
                "exact": te.window.exact.isoformat() if te.window.exact else None,
                "center": te.window.center.isoformat() if te.window.center else None,
                "label": label,
                "inferred": te.inferred,
            }
            put("time", payload, "Time")
        elif ANYTIME_RE.search(norm) and (nxt == "time" or bare_time):
            tr.entity("time", "any time")
            put(
                "time",
                {
                    "start": "06:00:00",
                    "end": "20:00:00",
                    "exact": None,
                    "center": None,
                    "label": self._("any time"),
                    "inferred": False,
                },
                "Time",
            )
        if set(changed) & {"service", "provider", "date", "time"}:
            st.options, st.pending = [], ({} if st.step != "CONFIRM" else st.pending)
        return changed

    def _window(self) -> TimeWindow | None:
        t = self.st.slots.get("time")
        if not t:
            return None
        f = time.fromisoformat
        return TimeWindow(
            f(t["start"]), f(t["end"]), f(t["exact"]) if t["exact"] else None, f(t["center"]) if t["center"] else None
        )

    def _validate_slots(self) -> str | None:
        """Apply clinic rules to what the caller asked for. Invalid values are dropped with an explanation."""
        st, tr, s, pol = self.st, self.tr, self.st.slots, self.rules.policy
        msgs: list[str] = []
        if st.flow == "book" and st.patient_id is not None and s.get("service"):
            sv = self.rules.service(s["service"])
            est = bool(st.established)
            if sv.requires == "new" and est:
                msgs.append(
                    self._(
                        "The {0} is only for first-time patients, and you've visited us before.", self.svc_name(sv.key)
                    )
                )
                tr.step(
                    "rule",
                    "Service eligibility",
                    f"{sv.key} requires a new patient; caller is established -> asking again",
                )
                s.pop("service")
            elif sv.requires == "established" and not est:
                s["service"] = pol.new_patient_service
                nsv = self.rules.service(pol.new_patient_service)
                msgs.append(
                    self._(
                        "As this is your first visit with us, I'll book a {0} instead, which takes {1} minutes.",
                        self.svc_name(nsv.key),
                        nsv.duration_minutes,
                    )
                )
                tr.step("rule", "Service eligibility", f"{sv.key} requires a past visit -> switched to {nsv.key}")
            elif not est and sv.key == pol.default_service_for_new_patients:
                s["service"] = pol.new_patient_service
                nsv = self.rules.service(pol.new_patient_service)
                msgs.append(
                    self._(
                        "As this is your first visit, I'll set it up as a {0}, which takes {1} minutes.",
                        self.svc_name(nsv.key),
                        nsv.duration_minutes,
                    )
                )
                tr.step("rule", "First-visit rule", f"{sv.key} -> {nsv.key} for patients with no past visits")
        svc_key, prov = s.get("service"), s.get("provider")
        if svc_key and prov and prov != "any" and svc_key not in self.rules.provider(prov).services:
            names = self.jor([self.pname(p.key) for p in self.rules.providers_for(svc_key)])
            msgs.append(self._("{0} doesn't offer a {1}. {2} can.", self.pname(prov), self.svc_name(svc_key), names))
            tr.step("rule", "Provider/service rule", f"{prov} does not offer {svc_key}")
            s.pop("provider")
            s.pop("provider_asked", None)
        d = s.get("date")
        if d and d != "earliest":
            day = date.fromisoformat(d)
            pkey = s.get("provider") if s.get("provider") not in (None, "any") else None
            reason = self.svc.validate_day(day, pkey)
            if reason:
                tr.step("rule", "Date rule", f"{day.isoformat()} rejected: {reason}")
                if reason == "past":
                    msgs.append(self._("That date has already passed."))
                elif reason == "too_far":
                    msgs.append(self._("I can only book up to {0} days ahead.", pol.max_advance_days))
                elif reason == "holiday":
                    msgs.append(self._("We're closed {0} for the holiday.", self._on(day)))
                elif reason == "closed_day":
                    open_days = self.wdays(sorted(self.rules.clinic_open_days()))
                    msgs.append(self._("We're closed on {0}s. We see patients on {1}.", self.wd(day), open_days))
                else:
                    p = self.rules.provider(pkey)  # type: ignore[arg-type]
                    days = [i for i, w in enumerate(WEEKDAYS) if p.schedule.get(w)]
                    msgs.append(
                        self._(
                            "{0} doesn't work on {1}s. They're in on {2}.",
                            self.pname(p.key),
                            self.wd(day),
                            self.wdays(days),
                        )
                    )
                s.pop("date")
        return " ".join(msgs) or None

    # ============================================================ options / offers ===
    def _opt(self, slot: Slot) -> dict[str, str]:
        return {"provider": slot.provider_key, "start": slot.start.isoformat()}

    def _read_options(self, options: list[dict[str, str]]) -> str:
        if not options:
            return self._("What day and time would you like?")
        slots = [(o["provider"], datetime.fromisoformat(o["start"])) for o in options]
        ords = self.ordinals()
        days = {s[1].date() for s in slots}
        provs = {s[0] for s in slots}
        if len(slots) == 1:
            k, dt = slots[0]
            return self._("{0} has {1} {2}. Would that work?", self.pname(k), self.ftime(dt), self._on(dt.date()))
        if len(days) == 1 and len(provs) == 1:
            k = next(iter(provs))
            times = self.jor([self.ftime(s[1]) for s in slots])
            return self._(
                "{0} has {1} {2}. Which would you like: the {3}?",
                self.pname(k),
                times,
                self._on(next(iter(days))),
                self.jor(ords[: len(slots)]),
            )
        if len(days) == 1:
            items = [self._("{0} with {1}", self.ftime(s[1]), self.pname(s[0])) for s in slots]
            return self._("{0} I have {1}. Which would you like?", cap(self._on(next(iter(days)))), self.jor(items))
        items = [
            self._("{0} at {1} with {2}", self.fdate(s[1].date()), self.ftime(s[1]), self.pname(s[0])) for s in slots
        ]
        return self._("I have {0}. Which would you like?", self.jor(items))

    def _compute_options(self) -> str:
        st, tr, s, pol = self.st, self.tr, self.st.slots, self.rules.policy
        service = s["service"]
        prov = s.get("provider") if s.get("provider") not in (None, "any") else None
        w = self._window()
        who = self.pname(prov) if prov else self._("any provider")
        st.pending = {k: v for k, v in st.pending.items() if k not in ("choice",)}
        if s["date"] == "earliest":
            found = self.svc.find_earliest(service, prov, w, pol.max_options)
            tr.step(
                "check",
                "Searched for earliest openings",
                f"{who}, next {pol.search_horizon_days} days -> {len(found)} found",
            )
            if not found:
                return self._no_availability(self._("I couldn't find any openings in the next few weeks."))
            st.options, st.step = [self._opt(x) for x in found], "OFFER"
            return self._("Here are the earliest openings. ") + self._read_options(st.options)
        day = date.fromisoformat(s["date"])
        day_slots = self.svc.slots_for_day(day, service, prov)
        tr.step("check", "Checked availability", f"{who} on {day.isoformat()} -> {len(day_slots)} open start times")
        if w and w.exact:
            exact_dt = datetime.combine(day, w.exact)
            match = next((x for x in day_slots if x.start == exact_dt), None)
            if match:
                tr.step(
                    "rule",
                    "Exact time free",
                    f"{self.ftime(exact_dt)} is open with {self.pname(match.provider_key)}",
                )
                st.options = [self._opt(match)]
                return self._to_confirm(match)
            if day_slots:
                near = sorted(day_slots, key=lambda x: abs((x.start - exact_dt).total_seconds()))[: pol.max_options]
                st.options, st.step = [self._opt(x) for x in sorted(near, key=lambda x: x.start)], "OFFER"
                tr.step("rule", "Exact time taken", "offering the closest open times that day")
                return self._(
                    "{0} isn't available {1}{2}. ",
                    self.ftime(exact_dt),
                    (self._("with {0}", who) + " ") if prov else "",
                    self._on(day),
                ) + self._read_options(st.options)
        elif w and w.center and day_slots:
            c = datetime.combine(day, w.center)
            pool = [x for x in day_slots if w.start <= x.start.time() <= w.end] or day_slots
            near = sorted(pool, key=lambda x: abs((x.start - c).total_seconds()))[: pol.max_options]
            st.options, st.step = [self._opt(x) for x in sorted(near, key=lambda x: x.start)], "OFFER"
            return self._read_options(st.options)
        elif day_slots:
            pool = [x for x in day_slots if not w or w.start <= x.start.time() <= w.end]
            if pool:
                picks = spread_pick(pool, pol.max_options)
                st.options, st.step = [self._opt(x) for x in picks], "OFFER"
                if len(picks) == 1:
                    tr.step("rule", "Single opening", "only one start time fits")
                return self._read_options(st.options)
            picks = spread_pick(day_slots, pol.max_options)
            st.options, st.step = [self._opt(x) for x in picks], "OFFER"
            return self._("Nothing is open then {0}. ", self._on(day)) + self._read_options(st.options)
        found = self.svc.find_earliest(service, prov, None, pol.max_options, after=day + timedelta(days=1))
        tr.step(
            "check",
            "Day is full",
            f"searched forward from {(day + timedelta(days=1)).isoformat()} -> {len(found)} found",
        )
        if not found:
            return self._no_availability(self._("There's nothing available {0}.", self._on(day)))
        st.options, st.step = [self._opt(x) for x in found], "OFFER"
        return self._("There's nothing available {0}. The next openings: ", self._on(day)) + self._read_options(
            st.options
        )

    def _no_availability(self, lead: str) -> str:
        st = self.st
        st.options = []
        offered = self._offer_waitlist(
            lead,
            self._(" Say 'any provider' to include everyone.") if st.slots.get("provider") not in (None, "any") else "",
        )
        if offered:
            return offered
        st.slots.pop("date", None)
        st.slots.pop("time", None)
        st.step = "COLLECT"
        hint = (
            self._(" Say 'any provider' to include everyone.") if st.slots.get("provider") not in (None, "any") else ""
        )
        return self._("{0}{1} Would you like to try a different day?", lead, hint)

    def _to_confirm(self, slot: Slot) -> str:
        st = self.st
        st.pending = {"kind": st.flow, "choice": self._opt(slot)}
        st.step = "CONFIRM"
        return self._confirm_prompt()

    def _confirm_prompt(self) -> str:
        st = self.st
        kind = st.pending.get("kind")
        if kind == "cancel":
            a = self.svc.get_appointment(st.target_id or -1)
            return (
                self._("Do you want to cancel your {0}?", self._describe(a))
                if a
                else self._("Do you want to cancel it?")
            )
        if kind == "late_continue":
            return self._("Do you still want to reschedule?")
        ch = st.pending.get("choice")
        if not ch:
            return self._("Shall I go ahead?")
        start = datetime.fromisoformat(ch["start"])
        prov = self.pname(ch["provider"])
        sv = self.rules.service(st.slots["service"])
        when = self._("{0} at {1}", self.fdate(start.date()), self.ftime(start))
        if kind == "reschedule":
            a = self.svc.get_appointment(st.target_id or -1)
            old = (
                self._("{0} at {1}", self.fdate(a.start_at.date()), self.ftime(a.start_at))
                if a
                else self._("its current time")
            )
            return self._(
                "I'll move your {0} from {1} to {2} with {3}. Shall I go ahead?", self.svc_name(sv.key), old, when, prov
            )
        return self._(
            "To confirm: a {0}-minute {1} with {2} {3} at {4}. Shall I book it?",
            sv.duration_minutes,
            self.svc_name(sv.key),
            prov,
            self._on(start.date()),
            self.ftime(start),
        )

    def _choose(self, opt: dict[str, str]) -> str:
        self.tr.step("rule", "Option chosen", f"{opt['provider']} at {opt['start']}")
        return self._to_confirm(Slot(opt["provider"], datetime.fromisoformat(opt["start"])))

    def _step_collect(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        r = self._maybe_interrupt(norm, dec, hit)
        if r:
            return r
        changed = self._absorb(norm)
        cleared = False
        if not changed:
            cleared = self._clear_mentioned(norm)
        if not changed and not cleared:
            return self._miss()
        msg = self._validate_slots()
        return (msg + " " if msg else "") + self._advance()

    _MENTION = {
        "time": r"\b(time|hour)\b",
        "date": r"\b(day|date)\b",
        "provider": r"\b(doctor|provider|dr|nurse|physician)\b",
        "service": r"\b(visit|service|type|reason)\b",
    }

    def _clear_mentioned(self, norm: str) -> bool:
        s = self.st.slots
        cleared = False
        for slot, pat in self._MENTION.items():
            if slot == "service" and self.st.flow != "book":
                continue
            if re.search(pat, norm) and s.get(slot) is not None:
                self.tr.step("correction", f"{slot.title()} cleared", "caller wants to change it")
                s.pop(slot)
                if slot == "provider":
                    s.pop("provider_asked", None)
                if slot == "date":
                    s.pop("week_of", None)
                cleared = True
        if cleared:
            self.st.options = []
        return cleared

    def _step_offer(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        r = self._maybe_interrupt(norm, dec, hit)
        if r:
            return r
        st = self.st
        idx = parse_choice(norm, len(st.options))
        if idx is not None:
            return self._choose(st.options[idx])
        dt = extract_datetime(norm, self.today, allow_bare_time=True)
        if dt.times and dt.times[-1].window.exact and not dt.dates:
            want = dt.times[-1].window.exact
            for o in st.options:
                if datetime.fromisoformat(o["start"]).time() == want:
                    return self._choose(o)
        changed = self._absorb(norm, bare_time=True)
        if changed:
            msg = self._validate_slots()
            return (msg + " " if msg else "") + self._advance()
        yn = yes_no(norm)
        if yn == "yes" and len(st.options) == 1:
            return self._choose(st.options[0])
        if yn == "yes":
            self.tr.step("note", "Yes to several options", "asking which one instead of guessing")
            return self._(
                "Which one would you like? Say the first, second, or third. {0}", self._read_options(st.options)
            )
        if yn == "no" or re.search(r"\b(none of|something else|different|other)\b", norm):
            offered = self._offer_waitlist()
            if offered:
                return offered
            st.slots.pop("time", None)
            st.options, st.step = [], "COLLECT"
            return self._("No problem. What day or time would suit you better?")
        return self._miss()

    # ================================================================== CONFIRM ===
    def _step_confirm(self, norm: str, dec: Decision, hit: FaqHit | None) -> str:
        r = self._maybe_interrupt(norm, dec, hit)
        if r:
            return r
        st = self.st
        kind = st.pending.get("kind")
        yn = yes_no(norm)
        if kind in ("book", "reschedule"):
            idx = parse_choice(norm, len(st.options)) if len(st.options) > 1 else None
            if idx is not None:
                self.tr.step("correction", "Different option chosen", f"switched to option {idx + 1}")
                return self._choose(st.options[idx])
            before = dict(st.pending)
            changed = self._absorb(norm)
            if changed:
                self.tr.step("correction", "Change during confirmation", "re-checking availability before asking again")
                st.pending = before if not changed else {}
                msg = self._validate_slots()
                return (msg + " " if msg else "") + self._advance()
            if yn == "yes":
                return self._do_book() if kind == "book" else self._do_reschedule()
            if yn == "no":
                st.options, st.pending, st.step = [], {}, "COLLECT"
                if self._clear_mentioned(norm):
                    return self._advance()
                return self._("No problem. What would you like to change: the day, the time, or the doctor?")
        elif kind == "cancel":
            if yn == "yes":
                return self._do_cancel()
            if yn == "no":
                st.step, st.pending = "MORE", {}
                return self._("Okay, I've kept your appointment. Is there anything else I can help with?")
        elif kind == "late_continue":
            if yn == "yes":
                st.pending = {}
                return self._advance()
            if yn == "no":
                st.step, st.pending = "MORE", {}
                return self._("No problem, your appointment stays as it is. Is there anything else I can help with?")
        return self._miss()

    def _arrival_note(self, service_key: str) -> str:
        pol = self.rules.policy
        first_visit = (not self.st.established) or service_key == pol.new_patient_service
        mins = pol.arrive_early_new_minutes if first_visit else pol.arrive_early_established_minutes
        return self._("Please arrive {0} minutes early and bring a photo ID and your insurance card.", mins)

    def _appt_payload(self, a: Appointment, action: str) -> dict[str, Any]:
        return {
            "id": a.id,
            "action": action,
            "description": self._describe(a),
            "start": a.start_at.isoformat(),
            "end": a.end_at.isoformat(),
            "service": a.service_key,
            "service_name": self.svc_name(a.service_key),
            "provider": a.provider.key,
            "provider_name": self.pfull(a.provider.key),
        }

    def _do_book(self) -> str:
        st, tr = self.st, self.tr
        ch = st.pending["choice"]
        start = datetime.fromisoformat(ch["start"])
        try:
            a = self.svc.book(st.patient_id or -1, ch["provider"], st.slots["service"], start, actor=self.actor)
        except PatientConflictError:
            tr.step("warn", "Patient overlap rule", "caller already has an appointment at that time")
            st.pending, st.options = {}, []
            st.slots.pop("time", None)
            st.step = "COLLECT"
            return self._("You already have an appointment at that time. Would you like a different time?")
        except SlotTakenError:
            tr.step(
                "warn", "Slot no longer free", "availability re-check at write time failed (someone else booked it)"
            )
            st.pending, st.options = {}, []
            return self._("I'm sorry, that time was just taken. ") + self._compute_options()
        a = self.svc.get_appointment(a.id)  # type: ignore[assignment]
        st.actions.append("booked")
        st.step, st.pending = "MORE", {}
        tr.action("book_appointment", f"appointment #{a.id} created")
        tr.step("rule", "Booked", "database unique index guarantees no double-booking")
        self._appointment = self._appt_payload(a, "booked")
        return self._(
            "You're booked: {0}. {1} We ask for {2} hours' notice if you need to change it. Is there anything else I can help with?",
            self._describe(a),
            self._arrival_note(a.service_key),
            self.rules.policy.cancel_notice_hours,
        )

    def _do_reschedule(self) -> str:
        st, tr = self.st, self.tr
        ch = st.pending["choice"]
        start = datetime.fromisoformat(ch["start"])
        old = self.svc.get_appointment(st.target_id or -1)
        late = bool(old and self.svc.is_late_change(old))
        try:
            a = self.svc.reschedule(st.target_id or -1, ch["provider"], start, actor=self.actor)
        except PatientConflictError:
            st.pending, st.options = {}, []
            st.slots.pop("time", None)
            st.step = "COLLECT"
            return self._("You already have another appointment at that time. Would you like a different time?")
        except BookingError:
            tr.step("warn", "Slot no longer free", "move rolled back, original appointment untouched")
            st.pending, st.options = {}, []
            return (
                self._("I'm sorry, that time was just taken, and your original appointment is unchanged. ")
                + self._compute_options()
            )
        a = self.svc.get_appointment(a.id)  # type: ignore[assignment]
        st.actions.append("rescheduled")
        st.step, st.pending = "MORE", {}
        tr.action("reschedule_appointment", f"appointment #{a.id} moved{' (late change flagged)' if late else ''}")
        self._appointment = self._appt_payload(a, "rescheduled")
        return self._(
            "Done. Your appointment is now {0}. {1} Is there anything else I can help with?",
            self._describe(a),
            self._arrival_note(a.service_key),
        )

    def _do_cancel(self) -> str:
        st, tr = self.st, self.tr
        a = self.svc.cancel(st.target_id or -1, actor=self.actor)
        st.actions.append("cancelled")
        st.step, st.pending = "MORE", {"offer": "book"}
        tr.action(
            "cancel_appointment", f"appointment #{a.id} cancelled{' (late: fee may apply)' if a.late_change else ''}"
        )
        self._appointment = self._appt_payload(a, "cancelled")
        fee = (
            self._(" Because it was inside the notice window, a late-cancellation fee may apply.")
            if a.late_change
            else ""
        )
        return self._(
            "Your appointment is cancelled.{0} Would you like to book a new one, or is there anything else?", fee
        )
