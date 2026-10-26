"""Serialisable dialogue state and the per-turn decision trace."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

PROGRESS_STEPS = ["Request", "Verify", "Details", "Confirm", "Done"]
STEP_TO_PROGRESS = {
    "ROUTE": 0,
    "CLARIFY": 0,
    "IDENTIFY": 1,
    "REGISTER": 1,
    "SELECT": 2,
    "COLLECT": 2,
    "OFFER": 2,
    "CONFIRM": 3,
    "WAITLIST": 2,
    "MORE": 4,
    "ENDED": 4,
}


@dataclass
class DialogState:
    step: str = "ROUTE"
    flow: str | None = None  # book | reschedule | cancel | list
    patient_id: int | None = None
    patient_first: str | None = None
    established: bool | None = None
    verify: dict[str, Any] = field(default_factory=lambda: {"first": None, "last": None, "dob": None, "attempts": 0})
    register: dict[str, Any] = field(default_factory=lambda: {"first": None, "last": None, "dob": None, "phone": None})
    slots: dict[str, Any] = field(default_factory=dict)  # service, provider, date, time, week_of, provider_asked
    options: list[dict[str, str]] = field(default_factory=list)  # [{"provider": key, "start": iso}]
    choices: list[int] = field(default_factory=list)  # appointment ids offered for selection
    target_id: int | None = None
    pending: dict[str, Any] = field(default_factory=dict)
    last_reply: str = ""
    misses: int = 0
    silences: int = 0
    turn: int = 0
    ended: bool = False
    end_reason: str | None = None  # goodbye | handoff | emergency | limit
    actions: list[str] = field(default_factory=list)  # booked, rescheduled, cancelled, listed, faq
    handoff_summary: str | None = None
    verified: bool = False
    lang: str = "en"  # en | ur : the language replies are spoken in
    ur_names: dict[str, str] = field(default_factory=dict)  # transliterated name -> how the caller said it in Urdu
    lang_locked: bool = False  # True when the caller picked it explicitly (UI toggle)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> DialogState:
        data = json.loads(raw or "{}")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def outcome(self) -> str:
        if self.end_reason in ("handoff", "emergency"):
            return self.end_reason
        if self.end_reason == "silence" and not self.actions:
            return "no_response"
        for a in ("booked", "rescheduled", "cancelled"):
            if a in self.actions:
                return a
        if "waitlisted" in self.actions:
            return "waitlisted"
        if "listed" in self.actions:
            return "listed"
        if "faq" in self.actions:
            return "faq"
        return "no_action"


class Trace:
    """Collects everything the brain decided in one turn, for the UI and the call log."""

    def __init__(self, turn: int, heard: str, normalized: str):
        self.data: dict[str, Any] = {
            "turn": turn,
            "heard": heard,
            "normalized": normalized,
            "intent": None,
            "entities": [],
            "steps": [],
            "slots": [],
            "state": {},
            "action": None,
            "reply": "",
        }

    def step(self, kind: str, label: str, detail: str = "") -> None:
        """kind: rule | check | correction | switch | note | warn"""
        self.data["steps"].append({"kind": kind, "label": label, "detail": detail})

    def entity(self, etype: str, value: str, raw: str = "") -> None:
        self.data["entities"].append({"type": etype, "value": value, "raw": raw})

    def action(self, name: str, result: str) -> None:
        self.data["action"] = {"name": name, "result": result}
