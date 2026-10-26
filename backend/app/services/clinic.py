"""Domain service: availability, booking, rescheduling, cancelling, patient lookup.

This is the only place that touches appointment tables. The dialogue brain and the staff API
both go through it, so every rule (opening hours, lead time, no overlap, notice window)
is enforced in one place.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app import models as m
from app.rules import ClinicRules
from app.textutil import normalize_name, phonetic_key

NAME_MATCH_THRESHOLD = 82


class BookingError(Exception):
    """Base class for booking failures that the caller can explain to a patient."""


class SlotTakenError(BookingError):
    pass


class PatientConflictError(BookingError):
    pass


class RuleViolationError(BookingError):
    pass


@dataclass(frozen=True)
class Slot:
    provider_key: str
    start: datetime


@dataclass(frozen=True)
class TimeWindow:
    """Constraint on appointment start times.

    `exact`  - the caller named one specific time ("3 pm").
    `center` - the caller wants something near a time ("around 3").
    """

    start: time
    end: time
    exact: time | None = None
    center: time | None = None


class ClinicService:
    def __init__(self, db: Session, rules: ClinicRules, now_fn: Callable[[], datetime] | None = None):
        self.db = db
        self.rules = rules
        self._now_fn = now_fn
        self._providers: dict[str, m.Provider] = {}
        self.on_freed: Callable[[list[Slot]], None] | None = None  # set by the app: called when slots become free
        self.waitlist = None  # WaitlistService, attached by make_service (None = feature off)

    # -- time ---------------------------------------------------------------------------
    def now(self) -> datetime:
        if self._now_fn:
            return self._now_fn()
        return datetime.now(self.rules.clinic.tz).replace(tzinfo=None)

    # -- providers ----------------------------------------------------------------------
    def sync_providers(self) -> None:
        """Upsert provider rows from the YAML rules."""
        existing = {p.key: p for p in self.db.scalars(select(m.Provider))}
        for rp in self.rules.providers:
            row = existing.get(rp.key)
            if row is None:
                row = m.Provider(key=rp.key)
                self.db.add(row)
            row.name, row.title, row.specialty, row.color = rp.name, rp.title, rp.specialty, rp.color
        self.db.commit()
        self._providers = {}

    def provider_row(self, key: str) -> m.Provider:
        if key not in self._providers:
            row = self.db.scalar(select(m.Provider).where(m.Provider.key == key))
            if row is None:
                raise KeyError(key)
            self._providers[key] = row
        return self._providers[key]

    def provider_key_for_id(self, provider_id: int) -> str:
        return self.db.get(m.Provider, provider_id).key  # type: ignore[union-attr]

    # -- patients -----------------------------------------------------------------------
    def find_patient(self, name: str, dob: date) -> tuple[m.Patient | None, int]:
        """Match on exact date of birth, then fuzzy name. Returns (patient, score)."""
        wanted = normalize_name(name)
        wanted_ph = phonetic_key(name).split()
        best: tuple[m.Patient | None, int] = (None, 0)
        for p in self.db.scalars(select(m.Patient).where(m.Patient.dob == dob)):
            score = int(fuzz.token_sort_ratio(wanted, normalize_name(p.full_name)))
            # speech-to-text spells names by ear: accept a phonetic match on first AND last name
            ph = phonetic_key(p.full_name).split()
            phonetic_hit = len(ph) >= 2 and len(wanted_ph) >= 2 and wanted_ph[0] == ph[0] and wanted_ph[-1] == ph[-1]
            if score < NAME_MATCH_THRESHOLD and phonetic_hit:
                score = NAME_MATCH_THRESHOLD
            if score > best[1]:
                best = (p, score)
        if best[1] >= NAME_MATCH_THRESHOLD:
            return best
        return None, best[1]

    # -- identity-check lockout (per phonetic name, so redialling does not reset it) --------
    def verification_locked(self, name: str) -> bool:
        row = self.db.get(m.VerifyAttempt, phonetic_key(name))
        return bool(row and row.locked_until and row.locked_until > m.utcnow())

    def record_verification_failure(self, name: str, max_failures: int, lock_minutes: int) -> bool:
        """Count a failed check. Returns True when this failure locks the name."""
        key = phonetic_key(name)
        row = self.db.get(m.VerifyAttempt, key)
        now = m.utcnow()
        if row is None:
            row = m.VerifyAttempt(key=key, failures=0)
            self.db.add(row)
        if row.locked_until and row.locked_until <= now:  # a finished lock starts a fresh count
            row.failures, row.locked_until = 0, None
        row.failures += 1
        row.updated_at = now
        locked = row.failures >= max_failures
        if locked:
            row.locked_until = now + timedelta(minutes=lock_minutes)
            self.db.add(
                m.AuditLog(
                    actor="system",
                    action="verify_lock",
                    entity="name_key",
                    entity_id=key[:40],
                    detail=f"{row.failures} failures",
                )
            )
        self.db.commit()
        return locked

    def clear_verification_failures(self, name: str) -> None:
        row = self.db.get(m.VerifyAttempt, phonetic_key(name))
        if row:
            self.db.delete(row)
            self.db.commit()

    def create_patient(self, first: str, last: str, dob: date, phone: str | None) -> m.Patient:
        p = m.Patient(first_name=first.strip().title(), last_name=last.strip().title(), dob=dob, phone=phone)
        self.db.add(p)
        self.db.commit()
        return p

    def is_established(self, patient_id: int) -> bool:
        """Established = has at least one past, non-cancelled visit."""
        q = select(func.count(m.Appointment.id)).where(
            m.Appointment.patient_id == patient_id,
            m.Appointment.status == "booked",
            m.Appointment.end_at <= self.now(),
        )
        return (self.db.scalar(q) or 0) > 0

    def upcoming(self, patient_id: int) -> list[m.Appointment]:
        q = (
            select(m.Appointment)
            .options(joinedload(m.Appointment.provider))
            .where(
                m.Appointment.patient_id == patient_id,
                m.Appointment.status == "booked",
                m.Appointment.start_at > self.now(),
            )
            .order_by(m.Appointment.start_at)
        )
        return list(self.db.scalars(q).unique())

    def get_appointment(self, appt_id: int) -> m.Appointment | None:
        q = select(m.Appointment).options(joinedload(m.Appointment.provider)).where(m.Appointment.id == appt_id)
        return self.db.scalars(q).unique().first()

    # -- availability -------------------------------------------------------------------
    def validate_day(self, day: date, provider_key: str | None = None) -> str | None:
        """Return a reason code if nothing can be booked on `day`, else None."""
        now = self.now()
        pol = self.rules.policy
        if day < now.date():
            return "past"
        if day > now.date() + timedelta(days=pol.max_advance_days):
            return "too_far"
        if self.rules.is_holiday(day):
            return "holiday"
        if day.weekday() not in self.rules.clinic_open_days():
            return "closed_day"
        if provider_key and not self.rules.provider(provider_key).windows(day.weekday()):
            return "provider_off"
        return None

    def _busy(self, provider_id: int, day: date) -> set[datetime]:
        lo = datetime.combine(day, time.min)
        q = select(m.AppointmentSlot.slot_start).where(
            m.AppointmentSlot.provider_id == provider_id,
            m.AppointmentSlot.slot_start >= lo,
            m.AppointmentSlot.slot_start < lo + timedelta(days=1),
        )
        return set(self.db.scalars(q))

    def slot_count(self, service_key: str) -> int:
        return math.ceil(self.rules.service(service_key).duration_minutes / self.rules.policy.slot_step_minutes)

    def starts_for_day(self, provider_key: str, day: date, service_key: str) -> list[datetime]:
        rp = self.rules.provider(provider_key)
        if service_key not in rp.services or self.validate_day(day, provider_key):
            return []
        dur = timedelta(minutes=self.rules.service(service_key).duration_minutes)
        step = timedelta(minutes=self.rules.policy.slot_step_minutes)
        earliest = self.now() + timedelta(minutes=self.rules.policy.min_lead_minutes)
        busy = self._busy(self.provider_row(provider_key).id, day)
        n = self.slot_count(service_key)
        out: list[datetime] = []
        for w_start, w_end in rp.windows(day.weekday()):
            t = datetime.combine(day, w_start)
            limit = datetime.combine(day, w_end)
            while t + dur <= limit:
                if t >= earliest and all(t + i * step not in busy for i in range(n)):
                    out.append(t)
                t += step
        return out

    def _day_load(self, day: date) -> dict[str, int]:
        lo = datetime.combine(day, time.min)
        q = (
            select(m.Provider.key, func.count(m.Appointment.id))
            .join(m.Appointment, m.Appointment.provider_id == m.Provider.id)
            .where(
                m.Appointment.status == "booked",
                m.Appointment.start_at >= lo,
                m.Appointment.start_at < lo + timedelta(days=1),
            )
            .group_by(m.Provider.key)
        )
        return dict(self.db.execute(q).all())

    def slots_for_day(
        self, day: date, service_key: str, provider_key: str | None, window: TimeWindow | None = None
    ) -> list[Slot]:
        """Unique start times on `day`. When several providers are free at one time the one
        with the lightest day wins (tie-break: order in clinic.yaml)."""
        keys = [provider_key] if provider_key else [p.key for p in self.rules.providers_for(service_key)]
        load = self._day_load(day)
        order = {p.key: i for i, p in enumerate(self.rules.providers)}
        by_start: dict[datetime, str] = {}
        for k in keys:
            for s in self.starts_for_day(k, day, service_key):
                if window and not (window.start <= s.time() <= window.end):
                    continue
                cur = by_start.get(s)
                if cur is None or (load.get(k, 0), order[k]) < (load.get(cur, 0), order[cur]):
                    by_start[s] = k
        return [Slot(k, s) for s, k in sorted(by_start.items())]

    def find_earliest(
        self,
        service_key: str,
        provider_key: str | None,
        window: TimeWindow | None,
        limit: int,
        after: date | None = None,
    ) -> list[Slot]:
        """Next available slots across days (used for "earliest available" and for fallbacks)."""
        out: list[Slot] = []
        day = after or self.now().date()
        for _ in range(self.rules.policy.search_horizon_days):
            if not self.validate_day(day, provider_key):
                day_slots = self.slots_for_day(day, service_key, provider_key, window)
                out.extend(spread_pick(day_slots, 1) if len(day_slots) > 0 else [])
            if len(out) >= limit:
                break
            day += timedelta(days=1)
        return out[:limit]

    # -- writes -------------------------------------------------------------------------
    def _audit(self, actor: str, action: str, appt: m.Appointment, detail: str = "") -> None:
        self.db.add(m.AuditLog(actor=actor, action=action, entity="appointment", entity_id=str(appt.id), detail=detail))

    def patient_is_free(self, patient_id: int, start: datetime, end: datetime) -> bool:
        try:
            self._check_patient_overlap(patient_id, start, end)
        except PatientConflictError:
            return False
        return True

    def _notify_freed(self, freed: list[Slot]) -> None:
        if self.on_freed and freed:
            try:
                self.on_freed(freed)
            except Exception:  # noqa: BLE001 - a failed waitlist offer must never undo a cancellation
                logging.getLogger("waitlist").exception("waitlist hook failed")

    def _check_patient_overlap(
        self, patient_id: int, start: datetime, end: datetime, ignore_id: int | None = None
    ) -> None:
        q = select(m.Appointment.id).where(
            m.Appointment.patient_id == patient_id,
            m.Appointment.status == "booked",
            m.Appointment.start_at < end,
            m.Appointment.end_at > start,
        )
        if ignore_id:
            q = q.where(m.Appointment.id != ignore_id)
        if self.db.scalar(q.limit(1)) is not None:
            raise PatientConflictError("patient already has an appointment at that time")

    def book(
        self, patient_id: int, provider_key: str, service_key: str, start: datetime, actor: str, via: str = "voice"
    ) -> m.Appointment:
        svc = self.rules.service(service_key)
        if start not in self.starts_for_day(provider_key, start.date(), service_key):
            raise SlotTakenError("that time is not available")
        end = start + timedelta(minutes=svc.duration_minutes)
        self._check_patient_overlap(patient_id, start, end)
        prov = self.provider_row(provider_key)
        step = timedelta(minutes=self.rules.policy.slot_step_minutes)
        appt = m.Appointment(
            patient_id=patient_id,
            provider_id=prov.id,
            service_key=service_key,
            start_at=start,
            end_at=end,
            created_via=via,
        )
        appt.slots = [
            m.AppointmentSlot(provider_id=prov.id, slot_start=start + i * step)
            for i in range(self.slot_count(service_key))
        ]
        self.db.add(appt)
        try:
            self.db.flush()
        except IntegrityError as exc:  # lost a race: the unique index caught the overlap
            self.db.rollback()
            raise SlotTakenError("that time was just taken") from exc
        self._audit(actor, "book", appt, f"{service_key} {provider_key} {start.isoformat()}")
        self.db.commit()
        return appt

    def reschedule(self, appt_id: int, provider_key: str, start: datetime, actor: str) -> m.Appointment:
        """Move an appointment atomically: old slots are freed and new ones taken in one
        transaction, so a failure leaves the original booking untouched."""
        appt = self.db.get(m.Appointment, appt_id)
        if appt is None or appt.status != "booked":
            raise RuleViolationError("appointment is not active")
        old = f"{self.provider_key_for_id(appt.provider_id)} {appt.start_at.isoformat()}"
        freed = [Slot(self.provider_key_for_id(appt.provider_id), sl.slot_start) for sl in appt.slots]
        svc = self.rules.service(appt.service_key)
        try:
            appt.slots.clear()
            self.db.flush()  # free our own slots first so moving by 15 minutes works
            if start not in self.starts_for_day(provider_key, start.date(), appt.service_key):
                raise SlotTakenError("that time is not available")
            end = start + timedelta(minutes=svc.duration_minutes)
            self._check_patient_overlap(appt.patient_id, start, end, ignore_id=appt.id)
            prov = self.provider_row(provider_key)
            step = timedelta(minutes=self.rules.policy.slot_step_minutes)
            appt.provider_id, appt.start_at, appt.end_at = prov.id, start, end
            appt.reschedule_count += 1
            appt.slots = [
                m.AppointmentSlot(provider_id=prov.id, slot_start=start + i * step)
                for i in range(self.slot_count(appt.service_key))
            ]
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise SlotTakenError("that time was just taken") from exc
        except BookingError:
            self.db.rollback()
            raise
        self._audit(actor, "reschedule", appt, f"{old} -> {provider_key} {start.isoformat()}")
        self.db.commit()
        self._notify_freed(freed)
        return appt

    def is_late_change(self, appt: m.Appointment) -> bool:
        return appt.start_at - self.now() < timedelta(hours=self.rules.policy.cancel_notice_hours)

    def cancel(self, appt_id: int, actor: str, reason: str = "patient request") -> m.Appointment:
        appt = self.db.get(m.Appointment, appt_id)
        if appt is None:
            raise RuleViolationError("appointment not found")
        if appt.status == "cancelled":
            return appt
        freed = [Slot(self.provider_key_for_id(appt.provider_id), sl.slot_start) for sl in appt.slots]
        appt.late_change = self.is_late_change(appt)
        appt.status = "cancelled"
        appt.cancelled_at = m.utcnow()
        appt.cancel_reason = reason[:80]
        appt.slots.clear()
        self._audit(actor, "cancel", appt, f"late={appt.late_change} reason={reason}")
        self.db.commit()
        self._notify_freed(freed)
        return appt


def spread_pick(slots: list[Slot], n: int) -> list[Slot]:
    """Pick up to n slots spread across the list (earliest, middle, latest...) so a caller
    hears a useful range instead of three back-to-back times."""
    if len(slots) <= n:
        return list(slots)
    if n == 1:
        return [slots[0]]
    idx = sorted({round(i * (len(slots) - 1) / (n - 1)) for i in range(n)})
    return [slots[i] for i in idx]


def group_by_day(slots: list[Slot]) -> dict[date, list[Slot]]:
    out: dict[date, list[Slot]] = defaultdict(list)
    for s in slots:
        out[s.start.date()].append(s)
    return out
