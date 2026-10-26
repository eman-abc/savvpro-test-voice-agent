"""Demo data. Everything here is FAKE: names, phone numbers (555-01xx) and dates of birth."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app import models as m
from app.config import Settings
from app.security import hash_password, verify_password
from app.services.clinic import ClinicService

log = logging.getLogger(__name__)

DEMO_PATIENTS = [
    # first, last, dob, phone, has past visit (established)
    ("Maria", "Gonzalez", date(1985, 3, 14), "5550101", True),
    ("James", "Carter", date(1972, 11, 2), "5550102", True),
    ("Priya", "Sharma", date(1990, 7, 23), "5550103", True),
    ("Liam", "O'Connor", date(1968, 1, 30), "5550104", True),
    ("Aisha", "Rahman", date(2001, 9, 9), "5550105", False),  # new patient, no history
    ("Wei", "Zhang", date(1995, 5, 17), "5550106", True),
    ("Olivia", "Johnson-Smith", date(1979, 12, 5), "5550107", True),
    ("Jose", "Hernandez", date(1988, 4, 21), "5550108", True),
    ("Ahmed", "Khan", date(1983, 6, 12), "5550109", True),
    ("Fatima", "Ali", date(1992, 2, 28), "5550110", True),
    ("Ayesha", "Siddiqui", date(1976, 9, 3), "5550111", False),  # new patient, no history
    ("Usman", "Malik", date(1999, 12, 19), "5550112", True),
]


def public_demo_hints() -> list[dict[str, str]]:
    """What the sign-in hint panel shows. Only used when DEMO_MODE is on."""
    notes = {
        "Maria": "established, has an upcoming visit",
        "James": "established, has an upcoming visit",
        "Priya": "established, has an upcoming visit",
        "Liam": "has a visit inside 24h (late-change rule)",
        "Aisha": "new patient, no history",
        "Wei": "established, nothing booked",
        "Olivia": "two upcoming visits (selection)",
        "Jose": "established, nothing booked",
        "Ahmed": "established, nothing booked (try Urdu)",
        "Fatima": "established, nothing booked (try Urdu)",
        "Ayesha": "new patient, no history (try Urdu)",
        "Usman": "established, nothing booked (try Urdu)",
    }
    return [
        {
            "name": f"{first} {last}",
            "dob": dob.strftime("%B %-d, %Y"),
            "note": notes[first],
        }
        for first, last, dob, _phone, _est in DEMO_PATIENTS
    ]


def _raw_visit(
    db: Session, svc: ClinicService, patient: m.Patient, provider_key: str, service_key: str, start: datetime
) -> None:
    prov = svc.provider_row(provider_key)
    step = timedelta(minutes=svc.rules.policy.slot_step_minutes)
    dur = timedelta(minutes=svc.rules.service(service_key).duration_minutes)
    appt = m.Appointment(
        patient_id=patient.id,
        provider_id=prov.id,
        service_key=service_key,
        start_at=start,
        end_at=start + dur,
        created_via="staff",
    )
    appt.slots = [
        m.AppointmentSlot(provider_id=prov.id, slot_start=start + i * step) for i in range(svc.slot_count(service_key))
    ]
    db.add(appt)


def _first_start(
    svc: ClinicService, provider_key: str, service_key: str, after_days: int, prefer: time = time(10, 0)
) -> datetime | None:
    day = svc.now().date() + timedelta(days=after_days)
    for _ in range(40):
        starts = svc.starts_for_day(provider_key, day, service_key)
        if starts:
            return min(starts, key=lambda s: abs(datetime.combine(day, prefer) - s))
        day += timedelta(days=1)
    return None


def seed_demo(svc: ClinicService, now: datetime | None = None) -> bool:
    """Insert demo patients and appointments if the database has no patients. Returns True if seeded."""
    db = svc.db
    if (db.scalar(select(func.count(m.Patient.id))) or 0) > 0:
        return False
    now = now or svc.now()
    patients: dict[str, m.Patient] = {}
    for first, last, dob, phone, established in DEMO_PATIENTS:
        p = m.Patient(first_name=first, last_name=last, dob=dob, phone=phone, is_demo=True)
        db.add(p)
        db.flush()
        patients[first] = p
        if established:  # a past, completed visit makes them an "established patient"
            past_day = (now - timedelta(days=60 + 7 * p.id)).date()
            while past_day.weekday() >= 5:
                past_day -= timedelta(days=1)
            past = datetime.combine(past_day, time([9, 10, 11, 14][p.id % 4], 0))
            _raw_visit(db, svc, p, "patel", "general_consultation", past)
    db.commit()

    plan = [
        ("Maria", "patel", "follow_up", 3),
        ("James", "nguyen", "annual_physical", 5),
        ("Priya", "kim", "vaccination", 2),
        ("Olivia", "patel", "follow_up", 4),
        ("Olivia", "kim", "lab_work", 6),
    ]
    for first, prov, service, offset in plan:
        start = _first_start(svc, prov, service, offset)
        if start:
            svc.book(patients[first].id, prov, service, start, actor="seed", via="staff")

    # Liam: a visit inside the 24h notice window, to demo the late-change rule.
    soon = [
        s
        for k in ("patel", "nguyen")
        for off in (0, 1)
        for s in svc.starts_for_day(k, now.date() + timedelta(days=off), "general_consultation")
        if timedelta(hours=2) <= s - now < timedelta(hours=23)
    ]
    start = min(soon) if soon else _first_start(svc, "patel", "general_consultation", 1)
    if start:
        prov = "patel" if start in svc.starts_for_day("patel", start.date(), "general_consultation") else "nguyen"
        svc.book(patients["Liam"].id, prov, "general_consultation", start, actor="seed", via="staff")
    log.info("seeded %d demo patients", len(patients))
    return True


def ensure_staff(db: Session, settings: Settings) -> None:
    user = db.scalar(select(m.StaffUser).where(m.StaffUser.username == settings.staff_username))
    if user is None:
        db.add(
            m.StaffUser(
                username=settings.staff_username,
                password_hash=hash_password(settings.staff_password, settings.bcrypt_rounds),
                role="admin",
            )
        )
    elif not verify_password(settings.staff_password, user.password_hash):
        user.password_hash = hash_password(
            settings.staff_password, settings.bcrypt_rounds
        )  # env var is the source of truth
        db.execute(update(m.StaffSession).where(m.StaffSession.user_id == user.id).values(revoked_at=m.utcnow()))
    db.commit()


def reset_demo(svc: ClinicService) -> None:
    """Wipe appointments, calls and patients, then re-seed. Admin-only (demo mode)."""
    db = svc.db
    for model in (
        m.Notification,
        m.WaitlistEntry,
        m.CallTurn,
        m.CallSession,
        m.AppointmentSlot,
        m.Appointment,
        m.AuditLog,
        m.VerifyAttempt,
        m.Patient,
    ):
        db.execute(delete(model))
    db.commit()
    seed_demo(svc)


def purge_old_calls(db: Session, hours: int) -> int:
    cutoff = m.utcnow() - timedelta(hours=hours)
    old = list(db.scalars(select(m.CallSession.id).where(m.CallSession.started_at < cutoff)))
    if old:
        db.execute(delete(m.CallTurn).where(m.CallTurn.call_id.in_(old)))
        db.execute(delete(m.CallSession).where(m.CallSession.id.in_(old)))
        db.commit()
    return len(old)
