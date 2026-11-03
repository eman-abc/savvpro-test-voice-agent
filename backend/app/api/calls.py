"""Public call endpoints: start a call and exchange turns with the brain."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.api.deps import AppContext, client_ip, get_ctx, get_db, get_service
from app.api.schemas import StartIn, TurnIn, TurnOut
from app.brain.dialog import TurnResult
from app.brain.state import PROGRESS_STEPS, STEP_TO_PROGRESS, DialogState
from app.services.clinic import ClinicService
from app.turns import brain_for, persist, run_turn

router = APIRouter(prefix="/api/calls", tags=["calls"])
log = logging.getLogger("calls")


def _replay(call: m.CallSession) -> TurnOut:
    """Rebuild the reply to the most recent turn (a client retry after a lost response)."""
    last = call.turns[-1]
    trace = json.loads(last.trace_json)
    st = DialogState.from_json(call.state_json)
    return TurnOut(
        call_id=call.id,
        reply=last.reply_text,
        trace=trace,
        progress={"steps": PROGRESS_STEPS, "current": STEP_TO_PROGRESS.get(st.step, 0), "verified": st.verified},
        ended=bool(call.ended_at),
        lang=st.lang,
        outcome=call.outcome,
        appointment=trace.get("appointment"),
    )


def _out(call_id: str, r: TurnResult, st: DialogState) -> TurnOut:
    return TurnOut(
        call_id=call_id,
        reply=r.reply,
        trace=r.trace,
        progress=r.progress,
        ended=r.ended,
        lang=st.lang,
        outcome=st.outcome() if r.ended else None,
        appointment=r.appointment,
    )


@router.post("", response_model=TurnOut, status_code=201)
def start_call(
    request: Request,
    body: StartIn | None = None,
    svc: ClinicService = Depends(get_service),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
) -> TurnOut:
    if not ctx.limiter.allow(f"newcall:{client_ip(request)}") and ctx.settings.app_env != "test":
        raise HTTPException(429, "Too many new calls from this address. Please wait a minute.")
    call_id = str(uuid.uuid4())
    st = DialogState()
    if body and body.lang == "ur":
        st.lang, st.lang_locked = "ur", True
    r = brain_for(svc, ctx, call_id).start(st)
    call = m.CallSession(id=call_id)
    db.add(call)
    db.flush()
    persist(db, call, None, r, st)
    log.info("call started", extra={"call_id": call_id})
    return _out(call_id, r, st)


@router.post("/{call_id}/turn", response_model=TurnOut)
def take_turn(
    call_id: str,
    body: TurnIn,
    svc: ClinicService = Depends(get_service),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
) -> TurnOut:
    call = db.scalar(select(m.CallSession).where(m.CallSession.id == call_id).with_for_update())
    if call is None:
        raise HTTPException(404, "Call not found")
    # Idempotency: the client says how many turns it has seen. A retry of the turn we already handled
    # gets the stored reply back instead of being processed twice (e.g. "yes" must not book twice).
    if (
        body.seq is not None
        and body.seq == call.turn_count - 1
        and call.turns
        and call.turns[-1].user_text == body.text
    ):
        ctx.metrics.inc("voice_turn_replays_total")
        return _replay(call)
    if body.seq is not None and body.seq != call.turn_count and not call.ended_at:
        raise HTTPException(409, "Out of sync with the server. Refresh the call.")
    if call.ended_at:
        raise HTTPException(409, "This call has ended")
    now = m.utcnow()
    if now - call.last_activity_at > timedelta(minutes=ctx.settings.call_idle_minutes):
        call.ended_at, call.outcome = now, "timeout"
        db.commit()
        raise HTTPException(410, "This call timed out. Please start a new one.")
    r, st = run_turn(db, svc, ctx, call, body.text, body.asr_confidence)
    log.info("turn", extra={"call_id": call_id})
    return _out(call_id, r, st)


@router.get("/{call_id}", response_model=dict)
def get_call(call_id: str, db: Session = Depends(get_db)) -> dict:
    call = db.get(m.CallSession, call_id)
    if call is None:
        raise HTTPException(404, "Call not found")
    return {
        "call_id": call.id,
        "ended": bool(call.ended_at),
        "outcome": call.outcome,
        "turns": [
            {"idx": t.idx, "user": t.user_text, "reply": t.reply_text, "trace": json.loads(t.trace_json)}
            for t in call.turns
        ],
    }
