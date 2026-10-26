"""SQLAlchemy models.

Double-booking is prevented by the database itself: every appointment owns one
`appointment_slots` row per 15-minute step it occupies, and (provider_id, slot_start) is
UNIQUE. Two callers racing for overlapping times cannot both succeed.
All datetimes are naive and expressed in the clinic's local timezone (see DESIGN.md).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    dob: Mapped[date] = mapped_column(Date, index=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_lang: Mapped[str] = mapped_column(String(2), default="en", server_default="en")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(20))
    specialty: Mapped[str] = mapped_column(String(80))
    color: Mapped[str] = mapped_column(String(9))


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    service_key: Mapped[str] = mapped_column(String(40))
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12), default="booked", index=True)  # booked|cancelled
    created_via: Mapped[str] = mapped_column(String(12), default="voice")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(80))
    late_change: Mapped[bool] = mapped_column(Boolean, default=False)
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)

    patient: Mapped[Patient] = relationship()
    provider: Mapped[Provider] = relationship()
    slots: Mapped[list[AppointmentSlot]] = relationship(back_populates="appointment", cascade="all, delete-orphan")


class AppointmentSlot(Base):
    __tablename__ = "appointment_slots"
    __table_args__ = (UniqueConstraint("provider_id", "slot_start", name="uq_provider_slot"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"))
    slot_start: Mapped[datetime] = mapped_column(DateTime)

    appointment: Mapped[Appointment] = relationship(back_populates="slots")


class StaffUser(Base):
    __tablename__ = "staff_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(12), default="staff")  # staff|admin


class CallSession(Base):
    __tablename__ = "call_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str | None] = mapped_column(String(24))  # booked|rescheduled|cancelled|faq|handoff|...
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"))
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    handoff_summary: Mapped[str | None] = mapped_column(Text)
    turn_count: Mapped[int] = mapped_column(Integer, default=0)

    turns: Mapped[list[CallTurn]] = relationship(
        back_populates="call", cascade="all, delete-orphan", order_by="CallTurn.idx"
    )


class CallTurn(Base):
    __tablename__ = "call_turns"
    __table_args__ = (Index("ix_turn_call_idx", "call_id", "idx"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("call_sessions.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    user_text: Mapped[str | None] = mapped_column(Text)
    reply_text: Mapped[str] = mapped_column(Text)
    trace_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    call: Mapped[CallSession] = relationship(back_populates="turns")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(60))  # "voice:<call id>" or "staff:<username>"
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text, default="")


class VerifyAttempt(Base):
    """Failed identity checks per (phonetic) name, so redialling cannot reset the guess counter."""

    __tablename__ = "verify_attempts"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StaffSession(Base):
    """Server-side record of each staff sign-in, so logout and password changes really revoke access."""

    __tablename__ = "staff_sessions"
    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class WaitlistEntry(Base):
    """A patient who wants an earlier/other slot. When a matching slot frees up they get an offer."""

    __tablename__ = "waitlist_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    service_key: Mapped[str] = mapped_column(String(40))
    provider_key: Mapped[str | None] = mapped_column(String(40))  # None = any provider
    date_from: Mapped[date] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date)
    time_from: Mapped[str | None] = mapped_column(String(5))  # "HH:MM"
    time_to: Mapped[str | None] = mapped_column(String(5))
    lang: Mapped[str] = mapped_column(String(2), default="en")
    status: Mapped[str] = mapped_column(
        String(12), default="waiting", index=True
    )  # waiting|offered|booked|cancelled|expired
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    offered_provider_key: Mapped[str | None] = mapped_column(String(40))
    offered_start: Mapped[datetime | None] = mapped_column(DateTime)
    offered_at: Mapped[datetime | None] = mapped_column(DateTime)
    offer_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    appointment_id: Mapped[int | None] = mapped_column(Integer)

    patient: Mapped[Patient] = relationship()


class Notification(Base):
    """The outbox: every message we send, and what the patient answered (two-way)."""

    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # reminder | offer | reply
    channel: Mapped[str] = mapped_column(String(12), default="outbox")
    to_phone: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(String(2), default="en")
    appointment_id: Mapped[int | None] = mapped_column(Integer)
    waitlist_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="sent", index=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    reply_text: Mapped[str | None] = mapped_column(Text)

    patient: Mapped[Patient] = relationship()
