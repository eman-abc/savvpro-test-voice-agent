"""Staff endpoints: calendar, appointments, call log with decision traces, stats."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app import models as m
from app.api.deps import AppContext, get_ctx, get_db, get_service, require_admin, require_staff
from app.api.schemas import CancelIn, ReplyIn, RunRemindersIn
from app.scenarios import load_scenarios, run_all
from app.seed import reset_demo
from app.services.clinic import ClinicService
from app.services.engagement import LogNotifier, send_due_reminders

router = APIRouter(prefix="/api/staff", tags=["staff"], dependencies=[Depends(require_staff)])


def _appt_out(a: m.Appointment, ctx: AppContext) -> dict:
    return {
        "id": a.id,
        "patient": a.patient.full_name,
        "patient_phone_last4": (a.patient.phone or "")[-4:] or None,
        "provider": a.provider.key,
        "provider_name": f"{a.provider.title} {a.provider.name}",
        "color": a.provider.color,
        "service": a.service_key,
        "service_name": ctx.rules.service(a.service_key).name,
        "start": a.start_at.isoformat(),
        "end": a.end_at.isoformat(),
        "status": a.status,
        "late_change": a.late_change,
        "reschedule_count": a.reschedule_count,
        "created_via": a.created_via,
        "cancel_reason": a.cancel_reason,
        "reminder_sent": a.reminder_sent_at is not None,
        "confirmed": a.confirmed_at is not None,
    }


@router.get("/providers")
def providers(ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return [
        {"key": p.key, "name": f"{p.title} {p.name}", "specialty": p.specialty, "color": p.color}
        for p in ctx.rules.providers
    ]


@router.get("/appointments")
def appointments(
    start: date | None = None,
    end: date | None = None,
    provider: str | None = None,
    include_cancelled: bool = False,
    db: Session = Depends(get_db),
    svc: ClinicService = Depends(get_service),
    ctx: AppContext = Depends(get_ctx),
) -> list[dict]:
    today = svc.now().date()
    start = start or today - timedelta(days=today.weekday())
    end = end or start + timedelta(days=7)
    if (end - start).days > 62:
        raise HTTPException(422, "Date range too large (max 62 days)")
    q = (
        select(m.Appointment)
        .options(joinedload(m.Appointment.patient), joinedload(m.Appointment.provider))
        .where(
            m.Appointment.start_at >= datetime.combine(start, time.min),
            m.Appointment.start_at < datetime.combine(end, time.min),
        )
        .order_by(m.Appointment.start_at)
    )
    if provider:
        q = q.where(m.Appointment.provider.has(key=provider))
    if not include_cancelled:
        q = q.where(m.Appointment.status == "booked")
    return [_appt_out(a, ctx) for a in db.scalars(q).unique()]


@router.post("/appointments/{appt_id}/cancel")
def cancel_appointment(
    appt_id: int,
    body: CancelIn,
    user: m.StaffUser = Depends(require_staff),
    svc: ClinicService = Depends(get_service),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    appt = svc.get_appointment(appt_id)
    if appt is None:
        raise HTTPException(404, "Appointment not found")
    svc.cancel(appt_id, actor=f"staff:{user.username}", reason=body.reason)
    return _appt_out(svc.get_appointment(appt_id), ctx)  # type: ignore[arg-type]


@router.get("/calls")
def calls(
    limit: int = Query(50, ge=1, le=200),
    outcome: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict]:
    q = select(m.CallSession).order_by(m.CallSession.started_at.desc()).limit(limit)
    if outcome:
        q = q.where(m.CallSession.outcome == outcome)
    return [
        {
            "id": c.id,
            "started_at": c.started_at.isoformat(),
            "turns": c.turn_count,
            "outcome": c.outcome or ("in_progress" if not c.ended_at else "no_action"),
            "handoff_summary": c.handoff_summary,
        }
        for c in db.scalars(q)
    ]


@router.get("/calls/{call_id}")
def call_detail(call_id: str, db: Session = Depends(get_db)) -> dict:
    c = db.get(m.CallSession, call_id)
    if c is None:
        raise HTTPException(404, "Call not found")
    return {
        "id": c.id,
        "started_at": c.started_at.isoformat(),
        "outcome": c.outcome,
        "handoff_summary": c.handoff_summary,
        "turns": [
            {"idx": t.idx, "user": t.user_text, "reply": t.reply_text, "trace": json.loads(t.trace_json)}
            for t in c.turns
        ],
    }


@router.get("/stats")
def stats(db: Session = Depends(get_db), svc: ClinicService = Depends(get_service)) -> dict:
    total = db.scalar(select(func.count(m.CallSession.id))) or 0
    by_outcome = dict(db.execute(select(m.CallSession.outcome, func.count()).group_by(m.CallSession.outcome)).all())
    turns = db.scalar(select(func.avg(m.CallSession.turn_count))) or 0
    resolved = sum(v for k, v in by_outcome.items() if k in ("booked", "rescheduled", "cancelled", "listed", "faq"))
    upcoming = db.scalar(
        select(func.count(m.Appointment.id)).where(m.Appointment.status == "booked", m.Appointment.start_at > svc.now())
    )
    wl = dict(db.execute(select(m.WaitlistEntry.status, func.count()).group_by(m.WaitlistEntry.status)).all())
    return {
        "waitlist_waiting": wl.get("waiting", 0) + wl.get("offered", 0),
        "waitlist_filled": wl.get("booked", 0),
        "now": svc.now().isoformat(timespec="minutes"),
        "calls_total": total,
        "outcomes": {(k or "in_progress"): v for k, v in by_outcome.items()},
        "avg_turns": round(float(turns), 1),
        "resolved_without_human": round(100 * resolved / total) if total else 0,
        "upcoming_appointments": upcoming or 0,
    }


@router.post("/demo/reset", dependencies=[Depends(require_admin)])
def demo_reset(svc: ClinicService = Depends(get_service), ctx: AppContext = Depends(get_ctx)) -> dict:
    if not ctx.settings.demo_mode:
        raise HTTPException(404, "Not available")
    reset_demo(svc)
    return {"ok": True}


@router.get("/scenarios")
def list_scenarios(ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return [
        {"id": s["id"], "title": s["title"], "tags": s.get("tags", []), "steps": len(s["steps"])}
        for s in load_scenarios(ctx.settings.scenarios_path)
    ]


@router.post("/scenarios/run")
def run_scenarios(ctx: AppContext = Depends(get_ctx)) -> dict:
    """Replay every golden conversation on throw-away databases and score the brain."""
    return run_all(ctx.rules, load_scenarios(ctx.settings.scenarios_path))


@router.get("/notifications")
def notifications(limit: int = Query(100, ge=1, le=300), db: Session = Depends(get_db)) -> list[dict]:
    q = (
        select(m.Notification)
        .options(joinedload(m.Notification.patient))
        .order_by(m.Notification.id.desc())
        .limit(limit)
    )
    return [
        {
            "id": n.id,
            "created_at": n.created_at.isoformat(),
            "patient": n.patient.full_name,
            "kind": n.kind,
            "body": n.body,
            "status": n.status,
            "lang": n.lang,
            "reply_text": n.reply_text,
            "appointment_id": n.appointment_id,
            "waitlist_id": n.waitlist_id,
            "phone_last4": n.to_phone[-4:],
        }
        for n in db.scalars(q).unique()
    ]


@router.post("/notifications/{note_id}/reply")
def simulate_reply(
    note_id: int, body: ReplyIn, db: Session = Depends(get_db), svc: ClinicService = Depends(get_service)
) -> dict:
    """Demo helper: answer a message as the patient would, going through exactly the same code as the webhook."""
    note = db.get(m.Notification, note_id)
    if note is None:
        raise HTTPException(404, "Message not found")
    reply = svc.waitlist.handle_inbound(note.to_phone, body.text)  # type: ignore[union-attr]
    return {"reply": reply}


@router.get("/waitlist")
def waitlist(db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    q = (
        select(m.WaitlistEntry)
        .options(joinedload(m.WaitlistEntry.patient))
        .order_by(m.WaitlistEntry.id.desc())
        .limit(200)
    )
    return [
        {
            "id": e.id,
            "patient": e.patient.full_name,
            "service": ctx.rules.service(e.service_key).name,
            "provider": e.provider_key,
            "date_from": e.date_from.isoformat(),
            "date_to": e.date_to.isoformat(),
            "time_from": e.time_from,
            "time_to": e.time_to,
            "status": e.status,
            "lang": e.lang,
            "created_at": e.created_at.isoformat(),
            "offered_start": e.offered_start.isoformat() if e.offered_start else None,
            "offer_expires_at": e.offer_expires_at.isoformat() if e.offer_expires_at else None,
        }
        for e in db.scalars(q).unique()
    ]


@router.post("/waitlist/{entry_id}/cancel")
def cancel_waitlist(entry_id: int, db: Session = Depends(get_db)) -> dict:
    e = db.get(m.WaitlistEntry, entry_id)
    if e is None:
        raise HTTPException(404, "Entry not found")
    if e.status in ("waiting", "offered"):
        e.status = "cancelled"
        db.commit()
    return {"id": e.id, "status": e.status}


@router.post("/reminders/run", dependencies=[Depends(require_admin)])
def run_reminders(body: RunRemindersIn, svc: ClinicService = Depends(get_service)) -> dict:
    """Send due reminders now (the background loop does this every minute when enabled)."""
    return {"sent": send_due_reminders(svc, LogNotifier(), body.hours), "expired": svc.waitlist.expire()}  # type: ignore[union-attr]
