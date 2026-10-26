"""Patient messaging: reminders, the waitlist, and handling replies (two-way).

Everything is deterministic rules, in English or Urdu:
  * a reminder asks "reply 1 to confirm, 2 to cancel"; the reply is interpreted by the same rules the
    voice brain uses (numbers, yes/no, Urdu, Roman Urdu);
  * a cancellation frees slots; the first waiting patient whose preferences fit gets an offer;
  * an offer expires, and the slot then goes to the next person in line.
Sending is behind a small interface, so an SMS/WhatsApp provider is an adapter, not a rewrite.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app import models as m
from app.brain import urdu
from app.brain.speech import fmt_time, on_date
from app.brain.text import normalize, yes_no
from app.rules import ClinicRules
from app.services.clinic import ClinicService, PatientConflictError, Slot, SlotTakenError

log = logging.getLogger("engagement")


class Notifier(Protocol):
    def send(self, phone: str, text: str) -> None: ...


class LogNotifier:
    """Default sender: logs a masked number instead of texting. An SMS/WhatsApp adapter implements `send`."""

    def send(self, phone: str, text: str) -> None:
        log.info("message (not actually sent) to ***-***-%s", phone[-4:])


# ---------------------------------------------------------------------------- wording ---
class Fmt:
    """Names, dates and times in the patient's language."""

    def __init__(self, rules: ClinicRules, lang: str, today: date):
        self.rules, self.lang, self.today = rules, lang if lang in ("en", "ur") else "en", today

    @property
    def clinic(self) -> str:
        c = self.rules.clinic
        return (c.name_ur or c.name) if self.lang == "ur" else c.name

    def service(self, key: str) -> str:
        s = self.rules.service(key)
        return (s.name_ur or s.name) if self.lang == "ur" else s.name

    def provider(self, key: str) -> str:
        p = self.rules.provider(key)
        return p.spoken_name_ur if self.lang == "ur" else p.spoken_name

    def when(self, dt: datetime) -> str:
        if self.lang == "ur":
            return f"{urdu.ur_on_date(dt.date(), self.today)} {urdu.ur_time(dt)}"
        return f"{on_date(dt.date(), self.today)} at {fmt_time(dt)}"


def say(lang: str, en: str, ur: str, **kw: object) -> str:
    return (ur if lang == "ur" else en).format(**kw)


def reminder_text(f: Fmt, appt: m.Appointment) -> str:
    return say(
        f.lang,
        "Reminder from {c}: {s} with {p} {w}. Reply 1 to confirm or 2 to cancel.",
        "{c} کی طرف سے یاددہانی: {p} کے ساتھ {s}، {w}۔ تصدیق کے لیے 1 اور منسوخی کے لیے 2 بھیجیں۔",
        c=f.clinic,
        s=f.service(appt.service_key),
        p=f.provider(appt.provider.key),
        w=f.when(appt.start_at),
    )


def offer_text(f: Fmt, entry: m.WaitlistEntry, minutes: int) -> str:
    assert entry.offered_provider_key and entry.offered_start
    return say(
        f.lang,
        "{c}: a {s} slot has opened {w} with {p}. Reply 1 to book it or 2 to skip. This offer expires in {n} minutes.",
        "{c}: {p} کے ساتھ {w} {s} کا وقت خالی ہوا ہے۔ بک کرنے کے لیے 1 اور چھوڑنے کے لیے 2 بھیجیں۔ یہ پیشکش {n} منٹ میں ختم ہو جائے گی۔",
        c=f.clinic,
        s=f.service(entry.service_key),
        p=f.provider(entry.offered_provider_key),
        w=f.when(entry.offered_start),
        n=minutes,
    )


# ----------------------------------------------------------------------------- outbox ---
class Outbox:
    """Writes every message to the database (so staff can see it) and hands it to the real sender."""

    def __init__(self, db, notifier: Notifier):
        self.db, self.notifier = db, notifier

    def send(self, patient: m.Patient, kind: str, body: str, lang: str, **links: int | None) -> m.Notification | None:
        if not patient.phone:
            return None
        note = m.Notification(patient_id=patient.id, kind=kind, to_phone=patient.phone, body=body, lang=lang, **links)
        self.db.add(note)
        self.db.flush()
        try:
            self.notifier.send(patient.phone, body)
        except Exception:  # noqa: BLE001 - the message stays in the outbox as a record
            log.exception("sending failed for notification %s", note.id)
            note.status = "failed"
        self.db.commit()
        return note


def send_due_reminders(svc: ClinicService, notifier: Notifier, hours: int) -> int:
    """Remind patients whose visit starts within `hours`. Each appointment is reminded at most once."""
    now = svc.now()
    outbox = Outbox(svc.db, notifier)
    q = (
        select(m.Appointment)
        .options(joinedload(m.Appointment.patient), joinedload(m.Appointment.provider))
        .where(
            m.Appointment.status == "booked",
            m.Appointment.reminder_sent_at.is_(None),
            m.Appointment.start_at > now,
            m.Appointment.start_at <= now + timedelta(hours=hours),
        )
    )
    sent = 0
    for a in list(svc.db.scalars(q).unique()):
        if not a.patient.phone:
            continue
        lang = a.patient.preferred_lang or "en"
        note = outbox.send(
            a.patient, "reminder", reminder_text(Fmt(svc.rules, lang, now.date()), a), lang, appointment_id=a.id
        )
        if note is None or note.status == "failed":
            continue  # retried on the next pass
        a.reminder_sent_at = m.utcnow()
        svc.db.add(m.AuditLog(actor="system", action="reminder", entity="appointment", entity_id=str(a.id)))
        sent += 1
    svc.db.commit()
    return sent


# --------------------------------------------------------------------------- waitlist ---
def _t(s: str | None) -> time | None:
    return time.fromisoformat(s) if s else None


class WaitlistService:
    def __init__(self, svc: ClinicService, outbox: Outbox, offer_minutes: int = 30):
        self.svc, self.outbox, self.offer_minutes = svc, outbox, offer_minutes
        self.db = svc.db

    # -- joining ----------------------------------------------------------------------
    def join(
        self,
        patient_id: int,
        service_key: str,
        provider_key: str | None,
        date_from: date,
        date_to: date,
        time_from: time | None = None,
        time_to: time | None = None,
        lang: str = "en",
    ) -> m.WaitlistEntry:
        existing = self.db.scalar(
            select(m.WaitlistEntry).where(
                m.WaitlistEntry.patient_id == patient_id,
                m.WaitlistEntry.service_key == service_key,
                m.WaitlistEntry.status.in_(["waiting", "offered"]),
            )
        )
        if existing:  # one place in line per patient and service: update the preferences instead
            existing.provider_key, existing.date_from, existing.date_to = provider_key, date_from, date_to
            existing.time_from = time_from.strftime("%H:%M") if time_from else None
            existing.time_to = time_to.strftime("%H:%M") if time_to else None
            existing.lang = lang
            self.db.commit()
            return existing
        e = m.WaitlistEntry(
            patient_id=patient_id,
            service_key=service_key,
            provider_key=provider_key,
            date_from=date_from,
            date_to=date_to,
            time_from=time_from.strftime("%H:%M") if time_from else None,
            time_to=time_to.strftime("%H:%M") if time_to else None,
            lang=lang,
        )
        self.db.add(e)
        self.db.commit()
        return e

    # -- matching ---------------------------------------------------------------------
    def _matches(self, e: m.WaitlistEntry, provider_key: str, start: datetime) -> bool:
        if e.status != "waiting" or not (e.date_from <= start.date() <= e.date_to):
            return False
        if e.provider_key and e.provider_key != provider_key:
            return False
        lo, hi = _t(e.time_from), _t(e.time_to)
        if (lo and start.time() < lo) or (hi and start.time() > hi):
            return False
        if start not in self.svc.starts_for_day(provider_key, start.date(), e.service_key):
            return False  # the slot is not actually bookable for this service (busy, too soon, wrong provider...)
        end = start + timedelta(minutes=self.svc.rules.service(e.service_key).duration_minutes)
        return self.svc.patient_is_free(e.patient_id, start, end)

    def on_slot_freed(self, freed: list[Slot], skip: set[int] | None = None) -> list[m.WaitlistEntry]:
        """Offer each freed start to the first matching person in line (first come, first served)."""
        skip = skip or set()
        waiting = list(
            self.db.scalars(
                select(m.WaitlistEntry)
                .options(joinedload(m.WaitlistEntry.patient))
                .where(m.WaitlistEntry.status == "waiting")
                .order_by(m.WaitlistEntry.created_at, m.WaitlistEntry.id)
            ).unique()
        )
        offered: list[m.WaitlistEntry] = []
        for slot in sorted(freed, key=lambda s: s.start):
            for e in waiting:
                if e.id in skip or e.status != "waiting":
                    continue
                if self._matches(e, slot.provider_key, slot.start):
                    self._offer(e, slot)
                    offered.append(e)
                    break
        return offered

    def _offer(self, e: m.WaitlistEntry, slot: Slot) -> None:
        now = self.svc.now()
        e.status, e.offered_provider_key, e.offered_start = "offered", slot.provider_key, slot.start
        e.offered_at, e.offer_expires_at = now, now + timedelta(minutes=self.offer_minutes)
        self.db.commit()
        f = Fmt(self.svc.rules, e.lang, now.date())
        self.outbox.send(e.patient, "offer", offer_text(f, e, self.offer_minutes), e.lang, waitlist_id=e.id)
        self.db.add(
            m.AuditLog(
                actor="system",
                action="waitlist_offer",
                entity="waitlist",
                entity_id=str(e.id),
                detail=slot.start.isoformat(),
            )
        )
        self.db.commit()

    # -- answers ----------------------------------------------------------------------
    def accept(self, e: m.WaitlistEntry) -> m.Appointment | None:
        assert e.offered_provider_key and e.offered_start
        try:
            appt = self.svc.book(
                e.patient_id, e.offered_provider_key, e.service_key, e.offered_start, actor="waitlist", via="waitlist"
            )
        except (SlotTakenError, PatientConflictError):
            self._reset(e)
            return None
        e.status, e.appointment_id = "booked", appt.id
        self.db.commit()
        return appt

    def decline(self, e: m.WaitlistEntry) -> list[m.WaitlistEntry]:
        slot = Slot(e.offered_provider_key, e.offered_start) if e.offered_provider_key and e.offered_start else None  # type: ignore[arg-type]
        self._reset(e)
        return self.on_slot_freed([slot], skip={e.id}) if slot else []

    def _reset(self, e: m.WaitlistEntry) -> None:
        e.status, e.offered_provider_key, e.offered_start, e.offered_at, e.offer_expires_at = (
            "waiting",
            None,
            None,
            None,
            None,
        )
        self.db.commit()

    def expire(self) -> int:
        """Lapse unanswered offers (the slot goes to the next person) and old requests."""
        now = self.svc.now()
        n = 0
        for e in list(
            self.db.scalars(
                select(m.WaitlistEntry).where(
                    m.WaitlistEntry.status == "offered", m.WaitlistEntry.offer_expires_at < now
                )
            )
        ):
            slot = Slot(e.offered_provider_key, e.offered_start)  # type: ignore[arg-type]
            for note in self.db.scalars(
                select(m.Notification).where(
                    m.Notification.waitlist_id == e.id, m.Notification.status == "sent", m.Notification.kind == "offer"
                )
            ):
                note.status = "expired"
            self._reset(e)
            self.on_slot_freed([slot], skip={e.id})
            n += 1
        for e in list(
            self.db.scalars(
                select(m.WaitlistEntry).where(m.WaitlistEntry.status == "waiting", m.WaitlistEntry.date_to < now.date())
            )
        ):
            e.status = "expired"
            n += 1
        self.db.commit()
        return n

    # -- replies ----------------------------------------------------------------------
    def handle_inbound(self, phone: str, text: str) -> str | None:
        """A patient replied to a reminder or an offer. Returns the reply we send back."""
        digits = re.sub(r"\D", "", phone)
        patient = (
            next(
                (
                    p
                    for p in self.db.scalars(select(m.Patient).where(m.Patient.phone.is_not(None)))
                    if re.sub(r"\D", "", p.phone or "")[-7:] == digits[-7:]
                ),
                None,
            )
            if len(digits) >= 7
            else None
        )
        if patient is None:
            return None
        lang = patient.preferred_lang or "en"
        f = Fmt(self.svc.rules, lang, self.svc.now().date())
        note = self.db.scalar(
            select(m.Notification)
            .where(
                m.Notification.patient_id == patient.id,
                m.Notification.kind.in_(["reminder", "offer"]),
                m.Notification.status.in_(["sent", "awaiting_confirm"]),
            )
            .order_by(m.Notification.id.desc())
        )
        phone_front = self.svc.rules.clinic.phone
        if note is None:
            reply = say(
                lang,
                "There's nothing waiting for your reply. Please call {p} for help.",
                "آپ کے جواب کا کوئی پیغام زیرِ انتظار نہیں ہے۔ مدد کے لیے {p} پر کال کریں۔",
                p=phone_front,
            )
            return self._answer(patient, reply, lang)
        note.replied_at, note.reply_text = m.utcnow(), text[:200]
        choice = interpret_reply(text)
        reply = (
            self._reply_offer(note, choice, f, lang)
            if note.kind == "offer"
            else self._reply_reminder(note, choice, f, lang, phone_front)
        )
        return self._answer(patient, reply, lang)

    def _answer(self, patient: m.Patient, reply: str, lang: str) -> str:
        self.db.commit()
        self.outbox.send(patient, "reply", reply, lang)
        return reply

    def _reply_reminder(self, note: m.Notification, choice: str | None, f: Fmt, lang: str, phone: str) -> str:
        appt = self.svc.get_appointment(note.appointment_id or -1)
        if appt is None or appt.status != "booked":
            note.status = "expired"
            return say(
                lang,
                "That appointment is no longer active. Please call {p}.",
                "وہ اپائنٹمنٹ اب فعال نہیں ہے۔ براہ کرم {p} پر کال کریں۔",
                p=phone,
            )
        if note.status == "awaiting_confirm":  # we warned about the late-cancellation fee and asked again
            if choice == "yes":
                self.svc.cancel(appt.id, actor="sms-reply", reason="cancelled by message reply (late)")
                note.status = "cancelled"
                return say(
                    lang,
                    "Your appointment has been cancelled. A late-cancellation fee may apply. Call {p} to rebook.",
                    "آپ کی اپائنٹمنٹ منسوخ کر دی گئی ہے۔ لیٹ کینسلیشن فیس لاگو ہو سکتی ہے۔ دوبارہ بک کرنے کے لیے {p} پر کال کریں۔",
                    p=phone,
                )
            if choice == "no":
                note.status = "confirmed"
                appt.confirmed_at = m.utcnow()
                return say(lang, "OK, we've kept your appointment.", "ٹھیک ہے، آپ کی اپائنٹمنٹ برقرار ہے۔")
            return say(
                lang,
                "Reply 1 to cancel anyway, or 2 to keep your appointment.",
                "پھر بھی منسوخ کرنے کے لیے 1 اور اپائنٹمنٹ رکھنے کے لیے 2 بھیجیں۔",
            )
        if choice == "yes":
            note.status, appt.confirmed_at = "confirmed", m.utcnow()
            self.db.add(m.AuditLog(actor="sms-reply", action="confirm", entity="appointment", entity_id=str(appt.id)))
            return say(lang, "Thank you, your appointment is confirmed.", "شکریہ، آپ کی اپائنٹمنٹ کی تصدیق ہو گئی ہے۔")
        if choice == "no":
            if self.svc.is_late_change(appt):
                note.status = "awaiting_confirm"
                hours = self.svc.rules.policy.cancel_notice_hours
                return say(
                    lang,
                    "That is within {h} hours, so a late-cancellation fee may apply. Reply 1 to cancel anyway, or 2 to keep it.",
                    "یہ {h} گھنٹوں کے اندر ہے، اس لیے لیٹ کینسلیشن فیس لاگو ہو سکتی ہے۔ پھر بھی منسوخ کرنے کے لیے 1 اور رکھنے کے لیے 2 بھیجیں۔",
                    h=hours,
                )
            self.svc.cancel(appt.id, actor="sms-reply", reason="cancelled by message reply")
            note.status = "cancelled"
            return say(
                lang,
                "Your appointment has been cancelled. Call {p} to rebook.",
                "آپ کی اپائنٹمنٹ منسوخ کر دی گئی ہے۔ دوبارہ بک کرنے کے لیے {p} پر کال کریں۔",
                p=phone,
            )
        return say(
            lang,
            "Sorry, I didn't understand. Reply 1 to confirm or 2 to cancel.",
            "معذرت، میں سمجھ نہیں سکا۔ تصدیق کے لیے 1 اور منسوخی کے لیے 2 بھیجیں۔",
        )

    def _reply_offer(self, note: m.Notification, choice: str | None, f: Fmt, lang: str) -> str:
        e = self.db.get(m.WaitlistEntry, note.waitlist_id or -1)
        if e is None or e.status != "offered" or (e.offer_expires_at and e.offer_expires_at < self.svc.now()):
            note.status = "expired"
            return say(
                lang,
                "Sorry, that offer has expired. You're still on the waitlist.",
                "معذرت، وہ پیشکش ختم ہو چکی ہے۔ آپ اب بھی ویٹنگ لسٹ میں ہیں۔",
            )
        if choice == "yes":
            appt = self.accept(e)
            if appt is None:
                note.status = "expired"
                return say(
                    lang,
                    "Sorry, that slot was just taken. You're still on the waitlist.",
                    "معذرت، وہ وقت ابھی کسی اور نے لے لیا ہے۔ آپ اب بھی ویٹنگ لسٹ میں ہیں۔",
                )
            note.status = "accepted"
            appt = self.svc.get_appointment(appt.id)
            return say(
                lang,
                "You're booked: {s} with {p} {w}. See you then.",
                "آپ کی اپائنٹمنٹ بک ہو گئی: {p} کے ساتھ {s}، {w}۔ ملاقات تک۔",
                s=f.service(appt.service_key),
                p=f.provider(appt.provider.key),
                w=f.when(appt.start_at),
            )  # type: ignore[union-attr]
        if choice == "no":
            note.status = "declined"
            self.decline(e)
            return say(
                lang, "No problem, we'll keep you on the waitlist.", "کوئی بات نہیں، ہم آپ کو ویٹنگ لسٹ میں رکھیں گے۔"
            )
        return say(
            lang,
            "Sorry, I didn't understand. Reply 1 to book the slot or 2 to skip it.",
            "معذرت، میں سمجھ نہیں سکا۔ وقت بک کرنے کے لیے 1 اور چھوڑنے کے لیے 2 بھیجیں۔",
        )


_ROMAN_YES = {"haan", "han", "haa", "ji", "jee", "theek", "bilkul", "zaroor", "hai"}
_ROMAN_NO = {"nahi", "nahin", "nai", "na"}


def interpret_reply(text: str) -> str | None:
    """ "yes" / "no" / None for a short message: 1, 2, yes, no, confirm, cancel, in English, Urdu or Roman Urdu."""
    t = text.strip().translate(str.maketrans("١٢۱۲", "1212"))
    if re.fullmatch(r"\W*1\W*", t):
        return "yes"
    if re.fullmatch(r"\W*2\W*", t):
        return "no"
    tokens = re.findall(r"[a-z]+", t.lower())
    if tokens and all(w in _ROMAN_YES for w in tokens):  # a bare "haan" / "ji" is too short to detect as Roman Urdu
        return "yes"
    if tokens and all(w in _ROMAN_NO for w in tokens):
        return "no"
    canon = normalize(urdu.to_canonical(t) if urdu.is_urdu(t) else t)
    yn = yes_no(canon)
    if yn:
        return yn
    if re.search(r"\b(cancel|cancelled|can't make it)\b", canon):
        return "no"
    if re.search(r"\bconfirm\w*\b", canon):
        return "yes"
    return None
