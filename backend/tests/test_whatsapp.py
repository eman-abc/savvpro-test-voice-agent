"""The WhatsApp channel against a fake Meta: signatures, chat over the brain, Urdu, voice notes, the 24-hour window,
reminders, waitlist offers and delivery receipts. Nothing here touches the real network."""

from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import models as m
from app.api.deps import make_service
from app.channels.whatsapp import (
    WhatsAppClient,
    WhatsAppError,
    WhatsAppNotifier,
    parse_webhook,
    verify_signature,
)
from app.main import create_app
from app.services.engagement import SkipSend
from tests.conftest import NOW, csrf, login, make_settings
from tests.wa_helpers import (
    APP_SECRET,
    ME,
    TOKEN,
    VERIFY,
    FakeMeta,
    audio_payload,
    image_payload,
    post_signed,
    sign,
    status_payload,
    text_payload,
)

CHAT_COUNTER = {"n": 0}


def wa_settings(**over):
    base = dict(
        whatsapp_verify_token=VERIFY,
        whatsapp_app_secret=APP_SECRET,
        whatsapp_access_token=TOKEN,
        whatsapp_phone_number_id="PNID",
        stt_provider="deepgram",
        deepgram_api_key="dg-key",
    )
    base.update(over)
    return make_settings(**base)


@pytest.fixture
def wa():
    meta = FakeMeta()
    with TestClient(create_app(wa_settings(), http_transport=meta.transport)) as c:
        yield c, meta


def say(c, text: str, sender: str = ME) -> None:
    CHAT_COUNTER["n"] += 1
    assert post_signed(c, text_payload(sender, text, f"wamid.IN{CHAT_COUNTER['n']}")).status_code == 200


def session(c):
    return c.app.state.ctx.db.session()


def link_patient_phone(c, first: str, phone: str = ME) -> int:
    login(c)
    pid = next(p["id"] for p in c.get("/api/staff/patients").json() if p["name"].startswith(first))
    assert c.post(f"/api/staff/patients/{pid}/phone", json={"phone": phone}, headers=csrf(c)).status_code == 200
    return pid


# ---------------------------------------------------------------- handshake and signature ---
def test_verification_handshake(wa):
    c, _ = wa
    ok = c.get(
        "/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "12345"}
    )
    assert ok.status_code == 200 and ok.text == "12345"
    for bad in (
        {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
        {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY},
        {},
    ):
        assert c.get("/api/whatsapp/webhook", params=bad).status_code == 403


def test_the_webhook_does_not_exist_until_whatsapp_is_configured(client):
    assert (
        client.get(
            "/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"}
        ).status_code
        == 404
    )
    assert client.post("/api/whatsapp/webhook", content=b"{}").status_code == 404
    assert client.get("/api/config").json()["whatsapp"] is False


def test_unsigned_or_wrongly_signed_calls_are_rejected_and_do_nothing(wa):
    c, meta = wa
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    assert c.post("/api/whatsapp/webhook", content=body).status_code == 401
    assert (
        c.post("/api/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=abc"}).status_code == 401
    )
    assert (
        c.post(
            "/api/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, "other-secret")}
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/api/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": "md5=" + sign(body)[7:]}
        ).status_code
        == 401
    )
    r = post_signed(c, text_payload(ME, "hello", "wamid.T"), tamper=True)  # body changed after signing
    assert r.status_code == 401 and meta.sent == []


def test_signature_function_matches_metas_scheme():
    body = b'{"a":1}'
    assert verify_signature("s", body, sign(body, "s"))
    assert not verify_signature("s", body, sign(body, "t"))
    assert not verify_signature("", body, sign(body, ""))  # no secret configured: nothing is trusted
    assert not verify_signature("s", body, None)


def test_valid_but_malformed_json_is_a_400(wa):
    c, _ = wa
    body = b"not json"
    assert c.post("/api/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)}).status_code == 400


def test_payload_parsing_covers_text_audio_other_and_statuses():
    msgs, _ = parse_webhook(text_payload("+92 300 1112222", "hi", "w1"))
    assert (msgs[0].kind, msgs[0].sender, msgs[0].text) == ("text", ME, "hi")
    msgs, _ = parse_webhook(audio_payload(ME, "M1", "w2"))
    assert (msgs[0].kind, msgs[0].media_id, msgs[0].mime) == ("audio", "M1", "audio/ogg; codecs=opus")
    assert parse_webhook(image_payload(ME, "w3"))[0][0].kind == "other"
    _, st = parse_webhook(
        status_payload("wamid.X", "failed", errors=[{"code": 131030, "title": "Recipient not in allowed list"}])
    )
    assert (st[0].status, st[0].error) == ("failed", "131030: Recipient not in allowed list")
    assert parse_webhook({}) == ([], []) and parse_webhook({"entry": [{"changes": [{}]}]}) == ([], [])


# -------------------------------------------------------------------------- chat over WhatsApp ---
def test_a_whole_booking_over_whatsapp_text(wa):
    c, meta = wa
    for line in ["I need a flu shot", "Wei Zhang, May 17th 1995", "any provider", "next tuesday at 2 pm"]:
        say(c, line)
    assert (
        "verify who you are" in meta.texts()[0]
        and "Thank you, Wei" in meta.texts()[1]
        and "Shall I book it" in meta.texts()[3]
    )
    say(c, "yes")
    assert "You're booked" in meta.texts()[-1]
    assert {b["to"] for b in meta.sent} == {ME}
    with session(c) as s:
        wei = s.scalar(select(m.Patient).where(m.Patient.first_name == "Wei"))
        appt = s.scalar(
            select(m.Appointment).where(
                m.Appointment.patient_id == wei.id, m.Appointment.start_at == datetime(2026, 11, 3, 14, 0)
            )
        )
        chat = s.scalar(select(m.CallSession).where(m.CallSession.channel == "whatsapp"))
        assert appt is not None and chat.contact == ME and chat.turn_count == 5


def test_the_chat_continues_in_one_session_and_a_new_one_starts_after_it_ends(wa):
    c, meta = wa
    say(c, "what time do you open")
    say(c, "no")  # ends the call
    say(c, "what time do you open")
    with session(c) as s:
        chats = list(
            s.scalars(
                select(m.CallSession).where(m.CallSession.channel == "whatsapp").order_by(m.CallSession.started_at)
            )
        )
    assert len(chats) == 2 and chats[0].ended_at is not None and chats[1].ended_at is None
    assert "8 AM to 5 PM" in meta.texts()[0] and "8 AM to 5 PM" in meta.texts()[-1]


def test_urdu_on_whatsapp(wa):
    c, meta = wa
    say(c, "مجھے فلو کا ٹیکہ چاہیے")
    say(c, "میرا نام وی ژانگ ہے")
    reply = meta.texts()[0]
    assert "شناخت" in reply and "verify who you are" not in reply
    say(c, "میری تاریخ پیدائش سترہ مئی انیس سو پچانوے ہے")
    assert "ویکسینیشن" in meta.texts()[-1]


def test_a_retried_webhook_is_processed_once(wa):
    c, meta = wa
    payload = text_payload(ME, "what time do you open", "wamid.SAME")
    post_signed(c, payload)
    post_signed(c, payload)  # Meta retries: the same message id arrives again
    assert len(meta.texts()) == 1
    assert 'whatsapp_messages_total{kind="text",result="duplicate"} 1' in c.get("/metrics").text


def test_a_duplicate_that_races_the_original_is_dropped_silently_not_apologised_for(wa):
    """On Postgres a retry can arrive while the original is still being processed; the unique key decides who wins."""
    c, meta = wa
    with session(c) as s:
        s.add(m.ProcessedMessage(id="wamid.RACE"))  # the "original" has already claimed this id
        s.commit()
    post_signed(c, text_payload(ME, "what time do you open", "wamid.RACE"))
    assert meta.sent == []  # no reply, and above all no "something went wrong"
    assert 'result="duplicate"' in c.get("/metrics").text


def test_messages_from_numbers_outside_the_allowed_list_are_ignored():
    meta = FakeMeta()
    with TestClient(create_app(wa_settings(whatsapp_allowed_numbers=f"+{ME}"), http_transport=meta.transport)) as c:
        say(c, "what time do you open", sender="923009998888")
        assert meta.sent == []
        say(c, "what time do you open", sender=ME)
        assert len(meta.texts()) == 1


def test_unsupported_message_types_get_a_polite_answer(wa):
    c, meta = wa
    post_signed(c, image_payload(ME, "wamid.IMG"))
    assert "text messages and voice notes" in meta.texts()[0] and "وائس نوٹ" in meta.texts()[0]


def test_a_failure_while_handling_a_message_gets_an_apology_not_a_crash(wa, monkeypatch):
    c, meta = wa
    import app.channels.whatsapp as wa_mod

    def boom(*a, **k):
        raise RuntimeError("brain exploded")

    monkeypatch.setattr(wa_mod, "run_turn", boom)
    say(c, "hello")
    assert "something went wrong" in meta.texts()[-1]


# ------------------------------------------------------------------------------ voice notes ---
def test_a_voice_note_is_transcribed_then_handled_like_text(wa):
    c, meta = wa
    meta.media["M1"] = b"OggS-opus-audio"
    post_signed(c, audio_payload(ME, "M1", "wamid.VN1"))
    reply = meta.texts()[0]
    assert reply.startswith("🎤 “what time do you open”") and "8 AM to 5 PM" in reply
    dg = meta.deepgram[0]
    assert (
        dg["params"]["language"] == "en"
        and dg["headers"]["content-type"] == "audio/ogg"
        and dg["size"] == len(b"OggS-opus-audio")
    )


def test_a_known_urdu_speaker_gets_urdu_recognition_first(wa):
    c, meta = wa
    pid = link_patient_phone(c, "Wei")
    with session(c) as s:
        s.get(m.Patient, pid).preferred_lang = "ur"
        s.commit()
    meta.media["M2"] = b"audio"
    meta.dg_queue = [{"transcript": "آپ کے اوقات کیا ہیں", "confidence": 0.9}]
    post_signed(c, audio_payload(ME, "M2", "wamid.VN2"))
    assert meta.deepgram[0]["params"]["language"] == "ur"
    assert "صبح 8 بجے" in meta.texts()[0]


def test_an_unsure_transcript_is_read_back_before_acting(wa):
    c, meta = wa
    say(c, "I need a flu shot")
    meta.media["M3"] = b"audio"
    meta.dg_queue = [
        {"transcript": "wei zhang may seventeen nineteen ninety five", "confidence": 0.3},
        {"transcript": "garbage", "confidence": 0.1},
    ]
    post_signed(c, audio_payload(ME, "M3", "wamid.VN3"))
    assert "I heard" in meta.texts()[-1] and "wei zhang may seventeen" in meta.texts()[-1]
    say(c, "yes")
    assert "Thank you, Wei" in meta.texts()[-1]


def test_a_voice_note_that_cannot_be_transcribed_gets_a_helpful_message(wa):
    c, meta = wa
    meta.media["M4"] = b"audio"
    meta.dg_status = 500
    post_signed(c, audio_payload(ME, "M4", "wamid.VN4"))
    assert "couldn't understand that voice note" in meta.texts()[-1] and "وائس نوٹ" in meta.texts()[-1]


def test_a_missing_media_file_is_handled(wa):
    c, meta = wa
    post_signed(c, audio_payload(ME, "NOPE", "wamid.VN5"))
    assert "couldn't understand that voice note" in meta.texts()[-1]


def test_voice_notes_without_a_speech_provider_say_so():
    meta = FakeMeta()
    with TestClient(create_app(wa_settings(stt_provider="none"), http_transport=meta.transport)) as c:
        post_signed(c, audio_payload(ME, "M6", "wamid.VN6"))
        assert "not enabled yet" in meta.texts()[0]


# ------------------------------------------------------------------ the 24-hour window and notifier ---
def run_reminders(c, hours=24 * 14):
    login(c)
    return c.post("/api/staff/reminders/run", json={"hours": hours}, headers=csrf(c)).json()


def notes(c, kind="reminder"):
    with session(c) as s:
        return list(s.scalars(select(m.Notification).where(m.Notification.kind == kind).order_by(m.Notification.id)))


def test_a_reminder_cannot_go_out_before_the_patient_has_written(wa):
    c, meta = wa
    link_patient_phone(c, "Maria")
    run_reminders(c)
    maria = next(n for n in notes(c) if n.to_phone == ME)
    assert maria.status == "window_closed" and "24-hour window" in maria.error and meta.sent == []


def test_once_the_patient_writes_the_reminder_goes_out_and_a_reply_confirms_it(wa):
    c, meta = wa
    link_patient_phone(c, "Maria")
    say(c, "hello")  # opens the 24-hour window
    sent_before = len(meta.sent)
    run_reminders(c)
    reminder = next(n for n in notes(c) if n.to_phone == ME)
    assert reminder.status == "sent" and reminder.delivery == "sent" and reminder.external_id.startswith("wamid.OUT")
    assert "Reply 1 to confirm" in meta.texts()[sent_before]
    say(c, "1")
    assert "confirmed" in meta.texts()[-1]
    with session(c) as s:
        maria = s.scalar(select(m.Patient).where(m.Patient.first_name == "Maria"))
        assert (
            s.scalar(
                select(m.Appointment).where(
                    m.Appointment.patient_id == maria.id, m.Appointment.confirmed_at.is_not(None)
                )
            )
            is not None
        )


def test_a_reply_inside_a_conversation_is_an_answer_not_a_reminder_reply(wa):
    c, meta = wa
    link_patient_phone(c, "Maria")
    say(c, "hello")
    run_reminders(c)
    say(c, "book a physical")  # now mid-conversation (identity step)
    say(c, "1")  # just the chat's own input, must not confirm the reminder
    with session(c) as s:
        assert s.scalar(select(m.Appointment).where(m.Appointment.confirmed_at.is_not(None))) is None


def test_reminders_retry_a_few_times_then_stop(wa):
    c, _ = wa
    link_patient_phone(c, "Maria")
    ctx = c.app.state.ctx
    for _ in range(5):
        with ctx.db.session() as s:
            from app.services.engagement import send_due_reminders

            svc = make_service(s, ctx.settings, ctx.rules, whatsapp=ctx.whatsapp)
            send_due_reminders(svc, svc.waitlist.outbox.notifier, 24 * 14, retry_after=timedelta(0))
    maria = [n for n in notes(c) if n.to_phone == ME]
    assert len(maria) == 3 and all(n.status == "window_closed" for n in maria)  # three attempts, then it gives up


def test_a_waitlist_offer_reaches_whatsapp_and_reply_1_books(wa):
    c, meta = wa
    pid = link_patient_phone(c, "Wei")
    say(c, "hello")
    ctx = c.app.state.ctx
    with ctx.db.session() as s:
        svc = make_service(s, ctx.settings, ctx.rules, whatsapp=ctx.whatsapp)
        svc.waitlist.join(pid, "vaccination", "kim", date(2026, 10, 28), date(2026, 10, 28))
    appts = c.get("/api/staff/appointments", params={"start": "2026-10-28", "end": "2026-10-29"}).json()
    victim = next(a for a in appts if a["service"] == "vaccination")  # Priya, Wed 10:00 with NP Kim
    c.post(f"/api/staff/appointments/{victim['id']}/cancel", json={}, headers=csrf(c))
    assert "Reply 1 to book" in meta.texts()[-1]
    say(c, "1")
    assert "You're booked" in meta.texts()[-1]
    with session(c) as s:
        booked = s.scalar(
            select(m.Appointment).where(m.Appointment.patient_id == pid, m.Appointment.created_via == "waitlist")
        )
        assert booked.start_at == datetime(2026, 10, 28, 10, 0)


def test_delivery_receipts_update_the_message_status(wa):
    c, meta = wa
    link_patient_phone(c, "Maria")
    say(c, "hello")
    run_reminders(c)
    wamid = next(n.external_id for n in notes(c) if n.to_phone == ME)
    post_signed(c, status_payload(wamid, "delivered"))
    post_signed(c, status_payload(wamid, "read"))
    post_signed(c, status_payload(wamid, "delivered"))  # a late, older receipt must not downgrade "read"
    assert next(n for n in notes(c) if n.external_id == wamid).delivery == "read"
    post_signed(c, status_payload(wamid, "failed", errors=[{"code": 131026, "title": "Message undeliverable"}]))
    failed = next(n for n in notes(c) if n.external_id == wamid)
    assert failed.delivery == "failed" and failed.status == "failed" and "131026" in failed.error
    post_signed(c, status_payload("wamid.UNKNOWN", "delivered"))  # a receipt for a message we do not know is ignored


def _notifier(meta, **over):
    s = wa_settings(**over)
    client = WhatsAppClient(TOKEN, "PNID", "v21.0", "https://graph.facebook.com", meta.transport)
    return client, s


def test_notifier_uses_a_template_when_the_window_is_closed(svc):
    meta = FakeMeta()
    client, settings = _notifier(meta, whatsapp_template_name="appointment_update")
    out = WhatsAppNotifier(client, svc.db, settings)
    wamid = out.send("+92 300 1112222", "Reminder from the clinic.\n\n   Reply 1 to confirm.")
    assert wamid == "wamid.OUT1"
    t = meta.sent[0]
    assert (
        t["type"] == "template"
        and t["template"]["name"] == "appointment_update"
        and t["template"]["language"]["code"] == "en"
    )
    param = t["template"]["components"][0]["parameters"][0]["text"]
    assert "\n" not in param and "   " not in param  # templates reject newlines and runs of spaces
    out.send(ME, "یاددہانی")
    assert meta.sent[1]["template"]["language"]["code"] == "ur"


def test_notifier_sends_free_form_inside_the_window_and_skips_outside_the_allowed_list(svc):
    meta = FakeMeta()
    client, settings = _notifier(meta, whatsapp_allowed_numbers=ME)
    svc.db.add(m.WhatsAppContact(phone=ME, last_inbound_at=m.utcnow() - timedelta(hours=23)))
    svc.db.commit()
    out = WhatsAppNotifier(client, svc.db, settings)
    out.send(ME, "hello")
    assert meta.sent[0]["type"] == "text" and meta.sent[0]["to"] == ME
    with pytest.raises(SkipSend) as skipped:
        out.send("923001234567", "hello")
    assert skipped.value.status == "skipped"
    svc.db.get(m.WhatsAppContact, ME).last_inbound_at = m.utcnow() - timedelta(hours=25)  # window just closed
    svc.db.commit()
    with pytest.raises(SkipSend) as closed:
        out.send(ME, "hello")
    assert closed.value.status == "window_closed"


def test_meta_error_codes_are_mapped(svc):
    meta = FakeMeta()
    client, settings = _notifier(meta)
    svc.db.add(m.WhatsAppContact(phone=ME))
    svc.db.commit()
    out = WhatsAppNotifier(client, svc.db, settings)
    meta.send_error = (131047, "Re-engagement message")  # Meta says the window is closed after all
    with pytest.raises(SkipSend) as e1:
        out.send(ME, "hi")
    assert e1.value.status == "window_closed"
    meta.send_error = (131030, "Recipient phone number not in allowed list")
    with pytest.raises(WhatsAppError) as e2:
        out.send(ME, "hi")
    assert e2.value.code == 131030


def test_graph_api_network_failure_is_a_clean_error(svc):
    def boom(request):
        raise httpx.ConnectError("down")

    client = WhatsAppClient(TOKEN, "PNID", "v21.0", "https://graph.facebook.com", httpx.MockTransport(boom))
    with pytest.raises(WhatsAppError, match="unreachable"):
        client.send_text(ME, "hi")
    with pytest.raises(WhatsAppError, match="media download failed"):
        client.download_media("M", 1000)


def test_oversized_media_is_refused(wa):
    c, meta = wa
    limit = c.app.state.ctx.settings.stt_max_bytes
    meta.media["BIG"] = b"x" * (limit + 1)
    post_signed(c, audio_payload(ME, "BIG", "wamid.BIG"))
    assert "couldn't understand that voice note" in meta.texts()[-1] and meta.deepgram == []


# ------------------------------------------------------------------------- linking the demo number ---
def test_staff_can_link_a_demo_patient_to_their_own_number(wa):
    c, _ = wa
    assert c.get("/api/staff/patients").status_code == 401
    login(c)
    pid = next(p["id"] for p in c.get("/api/staff/patients").json() if p["name"] == "Wei Zhang")
    assert c.post(f"/api/staff/patients/{pid}/phone", json={"phone": "+92 300 1112222"}).status_code == 403  # CSRF
    assert c.post(f"/api/staff/patients/{pid}/phone", json={"phone": "12"}, headers=csrf(c)).status_code == 422
    assert c.post("/api/staff/patients/9999/phone", json={"phone": ME}, headers=csrf(c)).status_code == 404
    ok = c.post(f"/api/staff/patients/{pid}/phone", json={"phone": "+92 300 1112222"}, headers=csrf(c))
    assert ok.json() == {"id": pid, "phone_last4": "2222"}
    assert next(p for p in c.get("/api/staff/patients").json() if p["id"] == pid)["phone_last4"] == "2222"
    assert "923001112222" not in c.get("/api/staff/patients").text  # only the last four digits are ever shown


def test_the_demo_number_from_the_environment_is_linked_at_startup():
    meta = FakeMeta()
    settings = wa_settings(demo_whatsapp_number="+92 300 1112222", demo_whatsapp_patient="Maria Gonzalez")
    with TestClient(create_app(settings, http_transport=meta.transport)) as c:
        login(c)
        maria = next(p for p in c.get("/api/staff/patients").json() if p["name"] == "Maria Gonzalez")
        assert maria["phone_last4"] == "2222"


def test_channel_status_endpoint(wa, client):
    c, _ = wa
    login(c)
    s = c.get("/api/staff/channels").json()
    assert s["whatsapp"]["enabled"] is True and s["whatsapp"]["webhook_path"] == "/api/whatsapp/webhook"
    assert s["speech_to_text"]["provider"] == "deepgram"
    login(client)
    assert client.get("/api/staff/channels").json()["speech_to_text"]["provider"] == "browser"
    assert APP_SECRET not in c.get("/api/staff/channels").text and TOKEN not in c.get("/api/config").text


def test_old_message_ids_are_purged_with_old_calls(wa):
    c, _ = wa
    say(c, "hello")
    from app.seed import purge_old_calls

    with session(c) as s:
        s.execute(m.ProcessedMessage.__table__.update().values(received_at=m.utcnow() - timedelta(hours=100)))
        s.commit()
        purge_old_calls(s, 48)
        assert s.scalar(select(m.ProcessedMessage)) is None


_ = NOW
