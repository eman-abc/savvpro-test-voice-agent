"""One conversational turn for any channel (web page, WhatsApp): load state, let the brain decide, save."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app import models as m
from app.brain.dialog import Brain, TurnResult
from app.brain.state import DialogState
from app.services.clinic import ClinicService

if TYPE_CHECKING:  # avoids a circular import with app.api.deps
    from app.api.deps import AppContext


def persist(db: Session, call: m.CallSession, user_text: str | None, r: TurnResult, st: DialogState) -> None:
    r.trace["appointment"] = r.appointment  # kept so a retried request can be answered identically
    call.state_json = st.to_json()
    call.last_activity_at = m.utcnow()
    call.turn_count += 1
    call.patient_id = st.patient_id
    db.add(
        m.CallTurn(
            call_id=call.id,
            idx=call.turn_count - 1,
            user_text=user_text,
            reply_text=r.reply,
            trace_json=json.dumps(r.trace),
        )
    )
    if r.ended:
        call.ended_at = m.utcnow()
        call.outcome = st.outcome()
        call.handoff_summary = st.handoff_summary
    db.commit()


def brain_for(svc: ClinicService, ctx: AppContext, call_id: str) -> Brain:
    s = ctx.settings
    return Brain(
        svc,
        ctx.faq,
        actor=f"voice:{call_id[:8]}",
        lock_failures=s.verify_lock_failures,
        lock_minutes=s.verify_lock_minutes,
        asr_min=s.asr_min_confidence,
    )


def observe(ctx: AppContext, r: TurnResult, st: DialogState, seconds: float) -> None:
    intent = r.trace.get("intent") or {}
    ctx.metrics.inc(
        "voice_turns_total", {"intent": intent.get("name", "none"), "decision": intent.get("decision", "none")}
    )
    ctx.metrics.observe("voice_turn_seconds", seconds)
    if any(s["label"] == "Not understood" for s in r.trace["steps"]):
        ctx.metrics.inc("voice_misses_total")
    if r.ended:
        ctx.metrics.inc("voice_calls_ended_total", {"outcome": st.outcome()})


def run_turn(
    db: Session,
    svc: ClinicService,
    ctx: AppContext,
    call: m.CallSession,
    text: str,
    asr_confidence: float | None = None,
) -> tuple[TurnResult, DialogState]:
    """One conversational turn for any channel (web page, WhatsApp): load state, let the brain decide, save."""
    st = DialogState.from_json(call.state_json)
    brain = brain_for(svc, ctx, call.id)
    started = time.perf_counter()
    if call.turn_count >= ctx.settings.call_max_turns:
        st.end_reason, st.ended, st.step = "limit", True, "ENDED"
        r = brain._finish(
            "We've reached the length limit for this call. Please call back or visit us in person. Goodbye.", "ENDED"
        )
    else:
        r = brain.handle(st, text, asr_confidence)
    persist(db, call, text, r, st)
    observe(ctx, r, st, time.perf_counter() - started)
    return r, st
