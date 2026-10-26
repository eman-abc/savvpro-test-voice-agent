"""Golden conversations: scripted calls with expectations, replayed against the brain.

Each scenario runs on its own throw-away in-memory database seeded with the demo data and a
pinned clock (Monday 2026-10-26 10:00), so results never depend on the real date or on live data.
They double as regression tests (tests/test_scenarios.py) and as a live scoreboard in the staff area.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.brain.dialog import Brain
from app.brain.retrieval import FaqIndex
from app.brain.state import DialogState
from app.config import Settings
from app.db import Database
from app.models import Base
from app.rules import ClinicRules
from app.seed import seed_demo
from app.services.clinic import ClinicService
from app.services.engagement import LogNotifier, Outbox, WaitlistService

PINNED_NOW = datetime(2026, 10, 26, 10, 0)


@dataclass
class StepResult:
    say: str
    reply: str
    ok: bool
    problems: list[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    id: str
    title: str
    tags: list[str]
    passed: bool
    turns: int
    intent_checks: int
    intent_hits: int
    failures: list[str]
    transcript: list[StepResult]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_scenarios(path: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(Path(path).glob("*.yaml")):
        out.extend(yaml.safe_load(f.read_text(encoding="utf-8")) or [])
    ids = [s["id"] for s in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario ids")
    return out


def _check(expect: dict[str, Any], r, st: DialogState) -> tuple[list[str], int, int]:
    problems: list[str] = []
    checks = hits = 0
    reply = r.reply.lower()
    if "step" in expect and st.step != expect["step"]:
        problems.append(f"expected step {expect['step']}, got {st.step}")
    if "intent" in expect:
        checks += 1
        got = (r.trace.get("intent") or {}).get("name")
        if got == expect["intent"]:
            hits += 1
        else:
            problems.append(f"expected intent {expect['intent']}, got {got}")
    if "decision" in expect:
        got = (r.trace.get("intent") or {}).get("decision")
        if got != expect["decision"]:
            problems.append(f"expected decision {expect['decision']}, got {got}")
    for needle in expect.get("reply_contains", []):
        if needle.lower() not in reply:
            problems.append(f"reply should contain {needle!r}")
    for needle in expect.get("reply_not_contains", []):
        if needle.lower() in reply:
            problems.append(f"reply should not contain {needle!r}")
    if "ended" in expect and r.ended != expect["ended"]:
        problems.append(f"expected ended={expect['ended']}")
    if "verified" in expect and st.verified != expect["verified"]:
        problems.append(f"expected verified={expect['verified']}")
    if "action" in expect and ((r.trace.get("action") or {}).get("name") != expect["action"]):
        problems.append(f"expected action {expect['action']}")
    for label, want in (expect.get("slots") or {}).items():
        have = next((s["value"] for s in r.trace["slots"] if s["label"] == label), None)
        if have is None or str(want).lower() not in str(have).lower():
            problems.append(f"slot {label} should contain {want!r}, has {have!r}")
    return problems, checks, hits


def _setup(svc: ClinicService, action: dict[str, Any]) -> None:
    """Scenario preconditions. `fill_day` books out a provider's whole day with throw-away visits."""
    from datetime import date, timedelta

    from app import models as m

    if "fill_day" in action:
        spec = action["fill_day"]
        day, key = date.fromisoformat(spec["date"]), spec["provider"]
        prov = svc.provider_row(key)
        filler = m.Patient(
            first_name="Filler", last_name="Patient", dob=date(1970, 1, 1), phone="5559999", is_demo=True
        )
        svc.db.add(filler)
        svc.db.flush()
        step = timedelta(minutes=svc.rules.policy.slot_step_minutes)
        for w_start, w_end in svc.rules.provider(key).windows(day.weekday()):
            t, end = datetime.combine(day, w_start), datetime.combine(day, w_end)
            while t < end:
                if not svc._busy(prov.id, day).intersection({t}):
                    a = m.Appointment(
                        patient_id=filler.id,
                        provider_id=prov.id,
                        service_key=spec.get("service", "vaccination"),
                        start_at=t,
                        end_at=t + step,
                        created_via="staff",
                    )
                    a.slots = [m.AppointmentSlot(provider_id=prov.id, slot_start=t)]
                    svc.db.add(a)
                    svc.db.flush()
                t += step
        svc.db.commit()


def run_scenario(rules: ClinicRules, sc: dict[str, Any]) -> ScenarioResult:
    db = Database(Settings(app_env="test", database_url="sqlite:///:memory:", bcrypt_rounds=4))
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        svc = ClinicService(session, rules, now_fn=lambda: PINNED_NOW)
        svc.sync_providers()
        seed_demo(svc, PINNED_NOW)
        svc.waitlist = WaitlistService(svc, Outbox(session, LogNotifier()))
        svc.on_freed = svc.waitlist.on_slot_freed
        for action in sc.get("setup", []):
            _setup(svc, action)
        brain = Brain(svc, FaqIndex(rules.faq), "scenario")
        st = DialogState(lang=sc.get("lang", "en"), lang_locked="lang" in sc)
        brain.start(st)
        transcript: list[StepResult] = []
        failures: list[str] = []
        checks = hits = 0
        for i, step in enumerate(sc["steps"], 1):
            r = brain.handle(st, step["say"])
            problems, c, h = _check(step.get("expect", {}), r, st)
            checks, hits = checks + c, hits + h
            transcript.append(StepResult(step["say"], r.reply, not problems, problems))
            if problems:
                failures += [f"step {i} ({step['say']!r}): {p}" for p in problems]
                break
        final = sc.get("final", {})
        if not failures:
            if "outcome" in final and st.outcome() != final["outcome"]:
                failures.append(f"expected outcome {final['outcome']}, got {st.outcome()}")
            if "ended" in final and st.ended != final["ended"]:
                failures.append(f"expected ended={final['ended']}")
        return ScenarioResult(
            sc["id"], sc["title"], sc.get("tags", []), not failures, st.turn, checks, hits, failures, transcript
        )


def run_all(rules: ClinicRules, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    results = [run_scenario(rules, s) for s in scenarios]
    passed = [r for r in results if r.passed]
    checks = sum(r.intent_checks for r in results)
    hits = sum(r.intent_hits for r in results)
    return {
        "total": len(results),
        "passed": len(passed),
        "completion_rate": round(100 * len(passed) / len(results)) if results else 0,
        "avg_turns": round(sum(r.turns for r in results) / len(results), 1) if results else 0,
        "intent_accuracy": round(100 * hits / checks) if checks else None,
        "results": [r.to_dict() for r in results],
    }
