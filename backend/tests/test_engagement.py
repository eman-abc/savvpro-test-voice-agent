"""Waitlist and two-way reminders: the no-show loop, end to end."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.brain import urdu
from app.main import create_app
from app.scenarios import _setup
from app.services.engagement import Outbox, WaitlistService, interpret_reply, send_due_reminders
from tests.conftest import NOW, Call, csrf, login, make_settings

NEXT_TUE = date(2026, 11, 3)
TEN = datetime(2026, 11, 3, 10, 0)


class Recorder:
    def __init__(self, fail_for: str | None = None):
        self.sent: list[tuple[str, str]] = []
        self.fail_for = fail_for

    def send(self, phone: str, text: str) -> None:
        if phone == self.fail_for:
            raise RuntimeError("provider down")
        self.sent.append((phone, text))


def patient(svc, first):
    return svc.db.scalar(select(m.Patient).where(m.Patient.first_name == first))


@pytest.fixture
def wl(svc):
    rec = Recorder()
    service = WaitlistService(svc, Outbox(svc.db, rec), offer_minutes=30)
    svc.waitlist = service
    svc.on_freed = service.on_slot_freed
    service.rec = rec  # type: ignore[attr-defined]
    return service


def booked_slot(svc, who="Maria", service="vaccination", prov="patel", start=TEN):
    return svc.book(patient(svc, who).id, prov, service, start, actor="t")


def join(wl, svc, who, **kw):
    args = dict(service_key="vaccination", provider_key="patel", date_from=NEXT_TUE, date_to=NEXT_TUE)
    args.update(kw)
    return wl.join(patient(svc, who).id, **args)


# ------------------------------------------------------------------------ the offer ---
def test_a_cancellation_offers_the_slot_to_the_first_person_waiting(svc, wl):
    appt = booked_slot(svc)
    entry = join(wl, svc, "Wei")
    svc.cancel(appt.id, actor="t")
    svc.db.refresh(entry)
    assert entry.status == "offered" and entry.offered_start == TEN and entry.offered_provider_key == "patel"
    phone, text = wl.rec.sent[-1]
    assert phone == "5550106" and "Reply 1 to book" in text and "10 AM" in text and "Maple Street" in text
    note = svc.db.scalar(select(m.Notification).where(m.Notification.kind == "offer"))
    assert note.waitlist_id == entry.id and note.status == "sent"


def test_first_come_first_served(svc, wl):
    appt = booked_slot(svc)
    first, second = join(wl, svc, "Wei"), join(wl, svc, "Jose")
    svc.cancel(appt.id, actor="t")
    svc.db.refresh(first)
    svc.db.refresh(second)
    assert first.status == "offered" and second.status == "waiting"


def test_preferences_must_match_the_freed_slot(svc, wl):
    appt = booked_slot(svc)
    wrong_day = join(wl, svc, "Wei", date_from=date(2026, 11, 4), date_to=date(2026, 11, 4))
    wrong_provider = join(wl, svc, "Jose", provider_key="kim")
    wrong_time = join(wl, svc, "Aisha", time_from=None)
    wrong_time.time_from, wrong_time.time_to = "13:00", "16:00"
    wrong_service = wl.join(
        patient(svc, "Priya").id, "lab_work", "patel", NEXT_TUE, NEXT_TUE
    )  # Dr. Patel does not offer lab work
    svc.db.commit()
    svc.cancel(appt.id, actor="t")
    for e in (wrong_day, wrong_provider, wrong_time, wrong_service):
        svc.db.refresh(e)
        assert e.status == "waiting", e.id
    assert wl.rec.sent == []


def test_any_provider_and_time_window_match(svc, wl):
    appt = booked_slot(svc)
    e = join(wl, svc, "Wei", provider_key=None)
    e.time_from, e.time_to = "09:00", "11:00"
    svc.db.commit()
    svc.cancel(appt.id, actor="t")
    svc.db.refresh(e)
    assert e.status == "offered"


def test_someone_who_already_has_a_visit_then_is_skipped(svc, wl):
    appt = booked_slot(svc)
    wei = patient(svc, "Wei")
    svc.book(wei.id, "nguyen", "general_consultation", datetime(2026, 11, 3, 9, 45), actor="t")  # overlaps 10:00
    a, b = join(wl, svc, "Wei"), join(wl, svc, "Jose")
    svc.cancel(appt.id, actor="t")
    svc.db.refresh(a)
    svc.db.refresh(b)
    assert a.status == "waiting" and b.status == "offered"


def test_a_patient_has_one_place_in_line_per_service(svc, wl):
    a = join(wl, svc, "Wei")
    b = join(wl, svc, "Wei", date_to=date(2026, 11, 5))
    assert a.id == b.id and b.date_to == date(2026, 11, 5)


def test_slots_too_soon_to_book_are_not_offered(svc, wl):
    soon = datetime(
        2026, 10, 26, 12, 0
    )  # inside the 2h minimum lead from NOW (10:00)? 12:00 is exactly allowed; use 11:00
    soon = datetime(2026, 10, 26, 11, 0)
    appt = svc.db.scalar(select(m.Appointment).where(m.Appointment.status == "booked", m.Appointment.start_at > NOW))
    assert appt is not None
    e = wl.join(patient(svc, "Wei").id, "vaccination", None, soon.date(), soon.date())
    from app.services.clinic import Slot

    assert wl.on_slot_freed([Slot("patel", soon)]) == []
    svc.db.refresh(e)
    assert e.status == "waiting"


# --------------------------------------------------------------------------- answers ---
def test_replying_1_books_the_slot(svc, wl):
    appt = booked_slot(svc)
    e = join(wl, svc, "Wei")
    svc.cancel(appt.id, actor="t")
    reply = wl.handle_inbound("555-0106", "1")
    assert "You're booked" in reply and "10 AM" in reply
    svc.db.refresh(e)
    booked = svc.get_appointment(e.appointment_id)
    assert e.status == "booked" and booked.start_at == TEN and booked.created_via == "waitlist"
    assert svc.db.scalar(select(m.Notification).where(m.Notification.kind == "offer")).status == "accepted"


def test_replying_2_passes_the_slot_to_the_next_person(svc, wl):
    appt = booked_slot(svc)
    first, second = join(wl, svc, "Wei"), join(wl, svc, "Jose")
    svc.cancel(appt.id, actor="t")
    reply = wl.handle_inbound("5550106", "2")
    assert "waitlist" in reply
    svc.db.refresh(first)
    svc.db.refresh(second)
    assert first.status == "waiting" and second.status == "offered"
    assert wl.rec.sent[-2][0] == "5550108"  # Jose got the offer (the last message is Wei's acknowledgement)


def test_an_offer_that_is_not_answered_expires_and_moves_on(svc, wl):
    appt = booked_slot(svc)
    first, second = join(wl, svc, "Wei"), join(wl, svc, "Jose")
    svc.cancel(appt.id, actor="t")
    svc._now_fn = lambda: NOW + timedelta(minutes=45)  # past the 30-minute offer
    assert wl.expire() >= 1
    svc.db.refresh(first)
    svc.db.refresh(second)
    assert first.status == "waiting" and second.status == "offered"
    assert (
        svc.db.scalar(
            select(m.Notification).where(m.Notification.waitlist_id == first.id, m.Notification.kind == "offer")
        ).status
        == "expired"
    )
    assert "expired" in wl.handle_inbound("5550106", "1") or "waiting for your reply" in wl.handle_inbound(
        "5550106", "1"
    )


def test_accepting_after_someone_else_took_the_slot_is_handled(svc, wl):
    appt = booked_slot(svc)
    e = join(wl, svc, "Wei")
    svc.cancel(appt.id, actor="t")
    svc.book(patient(svc, "Jose").id, "patel", "vaccination", TEN, actor="walk-in")  # grabbed before the reply
    reply = wl.handle_inbound("5550106", "1")
    assert "just taken" in reply
    svc.db.refresh(e)
    assert e.status == "waiting"


def test_expiry_also_retires_old_requests(svc, wl):
    e = join(wl, svc, "Wei")
    svc._now_fn = lambda: NOW + timedelta(days=30)
    wl.expire()
    svc.db.refresh(e)
    assert e.status == "expired"


# ------------------------------------------------------------------------- reminders ---
def test_reminders_are_stored_in_the_outbox_in_the_patients_language(svc):
    rec = Recorder()
    patient(svc, "Priya").preferred_lang = "ur"
    svc.db.commit()
    assert send_due_reminders(svc, rec, hours=24 * 14) >= 3
    notes = {
        n.patient.first_name: n for n in svc.db.scalars(select(m.Notification).where(m.Notification.kind == "reminder"))
    }
    assert "Reply 1 to confirm or 2 to cancel" in notes["Maria"].body and notes["Maria"].lang == "en"
    ur = notes["Priya"].body
    assert urdu.has_urdu_script(ur) and "میپل اسٹریٹ فیملی کلینک" in ur and "1" in ur and "نرس کم" in ur
    assert all(n.appointment_id for n in notes.values())


def test_a_failed_send_is_retried_and_recorded(svc):
    liam = patient(svc, "Liam")
    assert send_due_reminders(svc, Recorder(fail_for=liam.phone), hours=24 * 14) >= 3
    failed = svc.db.scalar(select(m.Notification).where(m.Notification.status == "failed"))
    assert failed is not None
    again = Recorder()
    assert send_due_reminders(svc, again, hours=24 * 14) == 1 and again.sent[0][0] == liam.phone


# --------------------------------------------------------------------- reminder replies ---
@pytest.fixture
def reminded(svc, wl):
    send_due_reminders(svc, wl.rec, hours=24 * 14)
    return wl


def appt_of(svc, first):
    return svc.db.scalar(
        select(m.Appointment).where(m.Appointment.patient_id == patient(svc, first).id, m.Appointment.start_at > NOW)
    )


def test_reply_1_confirms(svc, reminded):
    reply = reminded.handle_inbound("5550101", "1")
    assert "confirmed" in reply
    svc.db.refresh(appt_of(svc, "Maria"))
    assert appt_of(svc, "Maria").confirmed_at is not None


@pytest.mark.parametrize("text", ["yes", "Yes please", "confirm"])
def test_other_ways_to_confirm_in_english(svc, reminded, text):
    assert "confirmed" in reminded.handle_inbound("5550101", text)


def test_reply_2_cancels_and_frees_the_slot_for_the_waitlist(svc, reminded):
    maria_appt = appt_of(svc, "Maria")  # Thursday 10:00 with Dr. Patel, a follow-up
    e = reminded.join(
        patient(svc, "Wei").id, "follow_up", "patel", maria_appt.start_at.date(), maria_appt.start_at.date(), lang="en"
    )
    reply = reminded.handle_inbound("5550101", "2")
    assert "cancelled" in reply
    svc.db.refresh(maria_appt)
    svc.db.refresh(e)
    assert maria_appt.status == "cancelled" and maria_appt.late_change is False
    assert (
        e.status == "offered" and e.offered_start == maria_appt.start_at
    )  # the freed slot went straight to the next person


def test_cancelling_inside_the_notice_window_asks_twice(svc, reminded):
    liam_appt = appt_of(svc, "Liam")  # a few hours from now: a late change
    reply = reminded.handle_inbound("5550104", "cancel")
    assert "late-cancellation fee" in reply
    svc.db.refresh(liam_appt)
    assert liam_appt.status == "booked"  # not cancelled yet
    assert "kept" in reminded.handle_inbound("5550104", "2") and liam_appt.status == "booked"


def test_confirming_the_late_cancellation_cancels_and_flags_it(svc, reminded):
    liam_appt = appt_of(svc, "Liam")
    reminded.handle_inbound("5550104", "2")
    reply = reminded.handle_inbound("5550104", "1")
    assert "fee may apply" in reply
    svc.db.refresh(liam_appt)
    assert liam_appt.status == "cancelled" and liam_appt.late_change is True


def test_unclear_reply_gets_help_and_changes_nothing(svc, reminded):
    reply = reminded.handle_inbound("5550101", "hmm maybe")
    assert "Reply 1 to confirm or 2 to cancel" in reply
    assert appt_of(svc, "Maria").status == "booked"


def test_unknown_numbers_and_nothing_pending(svc, reminded):
    assert reminded.handle_inbound("0000000", "1") is None
    assert "nothing waiting" in reminded.handle_inbound("5550106", "1")  # Wei has no reminder or offer


@pytest.mark.parametrize(
    "text,outcome", [("ہاں", "confirmed"), ("جی ہاں", "confirmed"), ("haan", "confirmed"), ("١", "confirmed")]
)
def test_confirming_in_urdu_roman_urdu_and_urdu_digits(svc, reminded, text, outcome):
    patient(svc, "Maria").preferred_lang = "ur"
    svc.db.commit()
    reply = reminded.handle_inbound("5550101", text)
    assert urdu.has_urdu_script(reply) and "تصدیق" in reply


@pytest.mark.parametrize("text", ["منسوخ", "نہیں", "nahi", "2", "۲", "منسوخ کریں"])
def test_cancelling_in_urdu_roman_urdu_and_urdu_digits(svc, reminded, text):
    patient(svc, "Maria").preferred_lang = "ur"
    svc.db.commit()
    reply = reminded.handle_inbound("5550101", text)
    appt = appt_of(svc, "Maria") or svc.db.scalar(
        select(m.Appointment).where(
            m.Appointment.patient_id == patient(svc, "Maria").id, m.Appointment.status == "cancelled"
        )
    )
    svc.db.refresh(appt)
    assert appt.status == "cancelled" and "منسوخ کر دی گئی" in reply


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1", "yes"),
        ("2", "no"),
        (" 1 ", "yes"),
        ("YES", "yes"),
        ("No thanks", "no"),
        ("cancel it", "no"),
        ("confirm", "yes"),
        ("haan ji", "yes"),
        ("nahi", "no"),
        ("ٹھیک ہے", "yes"),
        ("random words", None),
        ("", None),
    ],
)
def test_interpret_reply(text, expected):
    assert interpret_reply(text) == expected


# -------------------------------------------------------------------------- the webhook ---
def webhook_client(secret="s3cret-webhook"):
    return TestClient(create_app(make_settings(inbound_secret=secret)))


def test_webhook_is_disabled_without_a_secret(client):
    assert client.post("/api/inbound/message", json={"from_phone": "5550101", "text": "1"}).status_code == 404


def test_webhook_rejects_bad_tokens_and_accepts_the_right_one():
    with webhook_client() as c:
        body = {"from_phone": "5550101", "text": "1"}
        assert c.post("/api/inbound/message", json=body).status_code == 401
        assert c.post("/api/inbound/message", json=body, headers={"X-Webhook-Token": "wrong"}).status_code == 401
        ok = c.post("/api/inbound/message", json=body, headers={"X-Webhook-Token": "s3cret-webhook"})
        assert ok.status_code == 200 and "nothing waiting" in ok.json()["reply"]
        assert (
            c.post(
                "/api/inbound/message",
                json={"from_phone": "1" * 99, "text": "x"},
                headers={"X-Webhook-Token": "s3cret-webhook"},
            ).status_code
            == 422
        )


def test_webhook_round_trip_confirms_a_reminder():
    with webhook_client() as c:
        login(c)
        c.post("/api/staff/reminders/run", json={"hours": 24 * 14}, headers=csrf(c))
        reply = c.post(
            "/api/inbound/message",
            json={"from_phone": "+92 555 0101", "text": "1"},
            headers={"X-Webhook-Token": "s3cret-webhook"},
        )
        assert "confirmed" in reply.json()["reply"]
        appts = c.get("/api/staff/appointments", params={"start": "2026-10-26", "end": "2026-11-09"}).json()
        assert any(a["patient"] == "Maria Gonzalez" and a["confirmed"] for a in appts)


# ----------------------------------------------------------------------- staff views ---
def test_staff_can_see_messages_reply_as_the_patient_and_manage_the_waitlist(client):
    assert client.get("/api/staff/notifications").status_code == 401
    login(client)
    assert client.post("/api/staff/reminders/run", json={"hours": 24 * 14}).status_code == 403  # CSRF
    run = client.post("/api/staff/reminders/run", json={"hours": 24 * 14}, headers=csrf(client)).json()
    assert run["sent"] >= 3
    notes = client.get("/api/staff/notifications").json()
    maria = next(n for n in notes if n["patient"] == "Maria Gonzalez" and n["kind"] == "reminder")
    assert "Reply 1" in maria["body"] and maria["status"] == "sent"
    reply = client.post(f"/api/staff/notifications/{maria['id']}/reply", json={"text": "1"}, headers=csrf(client))
    assert "confirmed" in reply.json()["reply"]
    after = {n["id"]: n for n in client.get("/api/staff/notifications").json()}
    assert after[maria["id"]]["status"] == "confirmed" and after[maria["id"]]["reply_text"] == "1"
    assert (
        client.post("/api/staff/notifications/99999/reply", json={"text": "1"}, headers=csrf(client)).status_code == 404
    )
    assert client.get("/api/staff/waitlist").json() == []
    stats = client.get("/api/staff/stats").json()
    assert stats["waitlist_waiting"] == 0 and stats["waitlist_filled"] == 0


def test_a_staff_cancellation_triggers_a_waitlist_offer_over_http(client):
    login(client)
    appts = client.get("/api/staff/appointments", params={"start": "2026-10-28", "end": "2026-10-29"}).json()
    vacc = next(a for a in appts if a["service"] == "vaccination")  # Priya with NP Kim, Wed 10:00
    ctx = client.app.state.ctx
    with ctx.db.session() as s:
        from app.api.deps import make_service
        from tests.conftest import NOW as now

        svc = make_service(s, ctx.settings, ctx.rules)
        wei = s.scalar(select(m.Patient).where(m.Patient.first_name == "Wei"))
        svc.waitlist.join(wei.id, "vaccination", "kim", date(2026, 10, 28), date(2026, 10, 28))
        assert now
    client.post(f"/api/staff/appointments/{vacc['id']}/cancel", json={}, headers=csrf(client))
    entries = client.get("/api/staff/waitlist").json()
    assert entries[0]["patient"] == "Wei Zhang" and entries[0]["status"] == "offered"
    assert client.get("/api/staff/stats").json()["waitlist_waiting"] == 1
    cancel = client.post(f"/api/staff/waitlist/{entries[0]['id']}/cancel", headers=csrf(client))
    assert cancel.json()["status"] == "cancelled"


# --------------------------------------------------------- the spoken waitlist story ---
@pytest.fixture
def full_wednesday(svc, wl):
    """Nurse Kim's Wednesday is completely booked, so a lab-work caller cannot get that day."""
    _setup(svc, {"fill_day": {"date": "2026-10-28", "provider": "kim"}})
    return wl


def say_all(call, *lines):
    for line in lines:
        call.say(line)


def test_declining_the_alternatives_offers_the_waitlist_and_joins_it(svc, full_wednesday, rules):
    c = Call(svc, rules)
    say_all(c, "book a lab test", "Wei Zhang, May 17th 1995", "wednesday", "any time")
    assert c.step == "OFFER" and "nothing available" in c.last.reply  # alternatives on other days
    reply = c.say("no")
    assert c.step == "WAITLIST" and "waitlist" in reply and "message you" in reply
    reply = c.say("yes")
    assert c.step == "MORE" and "on the waitlist" in reply and c.state.outcome() == "waitlisted"
    entry = svc.db.scalar(select(m.WaitlistEntry))
    assert (
        entry.status == "waiting"
        and entry.provider_key is None
        and entry.date_from == date(2026, 10, 28) == entry.date_to
    )
    assert entry.service_key == "lab_work" and entry.lang == "en"


def test_the_whole_loop_waitlist_then_cancellation_then_reply_1(svc, full_wednesday, rules):
    c = Call(svc, rules)
    say_all(c, "book a lab test", "Wei Zhang, May 17th 1995", "wednesday", "any time", "no", "yes")
    filler = svc.db.scalar(
        select(m.Appointment).where(
            m.Appointment.start_at == datetime(2026, 10, 28, 10, 0),
            m.Appointment.provider_id == svc.provider_row("kim").id,
        )
    )
    svc.cancel(filler.id, actor="staff:test")  # someone cancels: the slot frees up
    entry = svc.db.scalar(select(m.WaitlistEntry))
    svc.db.refresh(entry)
    assert entry.status == "offered" and entry.offered_start == datetime(2026, 10, 28, 10, 0)
    assert "Reply 1 to book" in full_wednesday.rec.sent[-1][1]
    reply = full_wednesday.handle_inbound("5550106", "1")
    assert "You're booked" in reply and "lab work visit" in reply
    mine = svc.db.scalar(
        select(m.Appointment).where(m.Appointment.patient_id == patient(svc, "Wei").id, m.Appointment.start_at > NOW)
    )
    assert mine.start_at == datetime(2026, 10, 28, 10, 0) and mine.created_via == "waitlist"


def test_asking_for_the_waitlist_directly(svc, wl, rules):
    c = Call(svc, rules)
    c.say("put me on the waitlist")
    assert c.state.slots.get("waitlist") is True and c.step == "IDENTIFY"
    c.say("Wei Zhang, May 17th 1995")
    c.say("a lab test")
    c.say("any")
    reply = c.say("next tuesday morning")
    assert c.step == "WAITLIST" and "waitlist" in reply
    c.say("yes")
    entry = svc.db.scalar(select(m.WaitlistEntry))
    assert entry.date_from == NEXT_TUE and (entry.time_from, entry.time_to) == ("08:00", "11:59")


def test_a_caller_can_say_no_to_the_waitlist_and_try_another_day(svc, full_wednesday, rules):
    c = Call(svc, rules)
    say_all(c, "book a lab test", "Wei Zhang, May 17th 1995", "wednesday", "any time", "no")
    reply = c.say("no")
    assert c.step == "COLLECT" and "try instead" in reply
    assert svc.db.scalar(select(m.WaitlistEntry)) is None


def test_urdu_waitlist_story_and_urdu_offer_message(svc, full_wednesday, rules):
    c = Call(svc, rules)
    c.state.lang = "ur"
    c.last = c.brain.start(c.state)
    say_all(c, "مجھے خون کا ٹیسٹ چاہیے", "وی ژانگ", "سترہ مئی انیس سو پچانوے", "بدھ", "کوئی بھی وقت")
    assert c.step == "OFFER"
    reply = c.say("نہیں")
    assert c.step == "WAITLIST" and "ویٹنگ لسٹ" in reply
    reply = c.say("جی ہاں")
    assert c.step == "MORE" and "ویٹنگ لسٹ" in reply and c.state.outcome() == "waitlisted"
    entry = svc.db.scalar(select(m.WaitlistEntry))
    assert entry.lang == "ur" and patient(svc, "Wei").preferred_lang == "ur"
    filler = svc.db.scalar(
        select(m.Appointment).where(
            m.Appointment.start_at == datetime(2026, 10, 28, 9, 0),
            m.Appointment.provider_id == svc.provider_row("kim").id,
        )
    )
    svc.cancel(filler.id, actor="staff:test")
    offer = full_wednesday.rec.sent[-1][1]
    assert urdu.has_urdu_script(offer) and "گریس" not in offer and "نرس کم" in offer and "1" in offer
    assert "بک ہو گئی" in full_wednesday.handle_inbound("5550106", "1")
