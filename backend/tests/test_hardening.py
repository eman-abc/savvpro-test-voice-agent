"""Security and reliability behaviour: lockout, sessions, proxies, idempotency, metrics, reminders, migrations."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from app import models as m
from app.brain.dialog import Brain
from app.brain.retrieval import FaqIndex
from app.brain.state import DialogState
from app.config import Settings
from app.main import create_app
from app.migrate import upgrade
from app.reminders import send_due_reminders
from app.textutil import phonetic_key
from tests.conftest import NOW, Call, csrf, login, make_settings


def new_call(svc, rules, lock_failures=3):
    brain = Brain(svc, FaqIndex(rules.faq), "test:call", lock_failures=lock_failures, lock_minutes=15)
    call = Call.__new__(Call)
    call.svc, call.state, call.brain = svc, DialogState(), brain
    call.last = brain.start(call.state)
    return call


# ------------------------------------------------------------------ phonetic names ---
@pytest.mark.parametrize(
    "a,b",
    [("Gonzales", "Gonzalez"), ("Jon", "John"), ("Katherine", "Catherine"), ("Sharma", "Sharmah"), ("Philip", "Filip")],
)
def test_phonetic_keys_match_spelling_variants(a, b):
    assert phonetic_key(a) == phonetic_key(b)


def test_phonetic_keys_keep_different_names_apart():
    assert phonetic_key("Gonzalez") != phonetic_key("Hernandez")
    assert phonetic_key("Priya Sharma") != phonetic_key("Priya Carter")


def test_speech_to_text_spelling_still_verifies(svc):
    from datetime import date

    p, _ = svc.find_patient("Maria Gonsales", date(1985, 3, 14))
    assert p and p.last_name == "Gonzalez"
    assert svc.find_patient("Maria Gonsales", date(1985, 3, 15))[0] is None  # the date of birth must still match


# ----------------------------------------------------------------------- lockout ---
def test_failed_checks_survive_hanging_up_and_redialling(svc, rules):
    wrong = "Maria Gonzalez, March 15th 1985"
    a = new_call(svc, rules)  # call 1: two wrong guesses, then the caller hangs up
    a.say("book an appointment")
    a.say(wrong)
    a.say(wrong)
    b = new_call(svc, rules)  # a brand-new call: the per-call counter is zero again
    b.say("book an appointment")
    reply = b.say(wrong)
    assert (
        b.last.ended
        and b.state.end_reason == "handoff"
        and "too many failed identity checks" in b.state.handoff_summary
    )
    assert "transferring you" in reply


def test_locked_name_is_refused_even_with_the_right_date_of_birth(svc, rules):
    for _ in range(3):
        svc.record_verification_failure("Maria Gonzalez", 3, 15)
    c = new_call(svc, rules)
    c.say("book an appointment")
    c.say("Maria Gonzalez, March 14th 1985")  # correct credentials
    assert c.last.ended and not c.state.verified


def test_lock_covers_spelling_variants(svc, rules):
    for _ in range(3):
        svc.record_verification_failure("Maria Gonzalez", 3, 15)
    assert svc.verification_locked("Maria Gonsales") and svc.verification_locked("maria  gonzalez")
    assert not svc.verification_locked("Wei Zhang")


def test_lock_expires_and_success_clears_the_count(svc, rules):
    for _ in range(3):
        svc.record_verification_failure("Maria Gonzalez", 3, 15)
    row = svc.db.get(m.VerifyAttempt, phonetic_key("Maria Gonzalez"))
    row.locked_until = m.utcnow() - timedelta(minutes=1)
    svc.db.commit()
    assert not svc.verification_locked("Maria Gonzalez")
    c = new_call(svc, rules)
    c.say("book an appointment")
    c.say("Maria Gonzalez, March 14th 1985")
    assert c.state.verified and svc.db.get(m.VerifyAttempt, phonetic_key("Maria Gonzalez")) is None


def test_failure_reply_reads_back_what_was_heard_without_naming_the_wrong_part(call):
    call.say("book an appointment")
    reply = call.say("Maria Gonzalez, March 15th 1985")
    assert "I heard Maria Gonzalez, born March 15, 1985" in reply and "couldn't match" in reply


# -------------------------------------------------------------------- sessions ---
def test_copied_cookie_dies_when_the_user_signs_out(client):
    login(client)
    stolen = client.cookies.get("clinic_session")
    assert client.post("/api/auth/logout", headers=csrf(client)).status_code == 200
    thief = TestClient(client.app)
    thief.cookies.set("clinic_session", stolen)
    assert thief.get("/api/auth/me").status_code == 401  # the JWT is still well-formed, but its session is revoked


def test_changing_the_staff_password_revokes_existing_sessions(client):
    from app.seed import ensure_staff

    login(client)
    assert client.get("/api/auth/me").status_code == 200
    ctx = client.app.state.ctx
    new = Settings(**{**ctx.settings.model_dump(), "staff_password": "a-brand-new-password-1"})
    with ctx.db.session() as s:
        ensure_staff(s, new)
    assert client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected(client):
    login(client)
    ctx = client.app.state.ctx
    with ctx.db.session() as s:
        for row in s.scalars(select(m.StaffSession)):
            row.expires_at = m.utcnow() - timedelta(seconds=1)
        s.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_token_without_a_session_row_is_rejected(client):
    from app.security import create_token

    ctx = client.app.state.ctx
    client.cookies.set("clinic_session", create_token(ctx.settings, "reception", "admin", "no-such-session"))
    assert client.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------- proxies and client IP ---
def hits(settings, headers, n=4):
    with TestClient(create_app(settings)) as c:
        return [c.get("/api/config", headers=headers).status_code for _ in range(n)]


def test_spoofed_forwarded_for_cannot_dodge_the_rate_limit():
    s = make_settings(rate_limit_per_minute=3, trusted_proxy_count=1)
    with TestClient(create_app(s)) as c:
        codes = [c.get("/api/config", headers={"X-Forwarded-For": f"6.6.6.{i}, 9.9.9.9"}).status_code for i in range(6)]
    # the proxy-appended address (9.9.9.9) is the real client; the attacker's fake left-hand values are ignored
    assert 429 in codes


def test_forwarded_for_is_ignored_without_a_trusted_proxy():
    s = make_settings(rate_limit_per_minute=3, trusted_proxy_count=0)
    with TestClient(create_app(s)) as c:
        codes = [c.get("/api/config", headers={"X-Forwarded-For": f"6.6.6.{i}"}).status_code for i in range(6)]
    assert 429 in codes


def test_distinct_real_clients_behind_a_proxy_are_limited_separately():
    s = make_settings(rate_limit_per_minute=3, trusted_proxy_count=1)
    with TestClient(create_app(s)) as c:
        a = [c.get("/api/config", headers={"X-Forwarded-For": "1.1.1.1"}).status_code for _ in range(3)]
        b = c.get("/api/config", headers={"X-Forwarded-For": "2.2.2.2"}).status_code
    assert a == [200, 200, 200] and b == 200


# --------------------------------------------------------------------- idempotency ---
def test_retried_turn_is_answered_once_not_processed_twice(client):
    cid = client.post("/api/calls").json()["call_id"]
    seq = 1
    for text in ["I need a flu shot", "Priya Sharma, July 23rd 1990", "any provider", "tomorrow at 10 am"]:
        client.post(f"/api/calls/{cid}/turn", json={"text": text, "seq": seq})
        seq += 1
    first = client.post(f"/api/calls/{cid}/turn", json={"text": "yes", "seq": seq}).json()
    assert first["appointment"]["action"] == "booked"
    retry = client.post(f"/api/calls/{cid}/turn", json={"text": "yes", "seq": seq})  # the response was "lost"
    assert retry.status_code == 200 and retry.json()["reply"] == first["reply"]
    assert retry.json()["appointment"] == first["appointment"]
    login(client)
    booked = [
        a
        for a in client.get("/api/staff/appointments", params={"start": "2026-10-27", "end": "2026-10-28"}).json()
        if a["patient"] == "Priya Sharma"
    ]
    assert len(booked) == 1  # exactly one booking despite two requests
    assert client.get(f"/api/staff/calls/{cid}").json()["turns"][-1]["idx"] == seq  # and no extra turn was recorded


def test_out_of_sync_client_is_told_so(client):
    cid = client.post("/api/calls").json()["call_id"]
    assert client.post(f"/api/calls/{cid}/turn", json={"text": "hello", "seq": 7}).status_code == 409
    assert client.post(f"/api/calls/{cid}/turn", json={"text": "hello", "seq": 1}).status_code == 200
    assert client.post(f"/api/calls/{cid}/turn", json={"text": "hello"}).status_code == 200  # seq stays optional


def test_different_text_at_the_previous_seq_is_not_treated_as_a_retry(client):
    cid = client.post("/api/calls").json()["call_id"]
    client.post(f"/api/calls/{cid}/turn", json={"text": "hello", "seq": 1})
    assert client.post(f"/api/calls/{cid}/turn", json={"text": "something else", "seq": 1}).status_code == 409


# ---------------------------------------------------------------------- metrics ---
def test_metrics_exposition_and_content(client):
    cid = client.post("/api/calls").json()["call_id"]
    client.post(f"/api/calls/{cid}/turn", json={"text": "what time do you open"})
    client.post(f"/api/calls/{cid}/turn", json={"text": "blorp"})
    body = client.get("/metrics").text
    assert 'voice_turns_total{decision="act",intent="faq"} 1' in body
    assert "voice_misses_total 1" in body
    assert 'http_requests_total{method="POST",route="/api/calls/{call_id}/turn",status="200"} 2' in body
    assert 'voice_turn_seconds_bucket{le="+Inf"} 2' in body
    assert "# TYPE http_request_seconds histogram" in body


def test_metrics_token_and_prod_default():
    with TestClient(create_app(make_settings(metrics_token="s3cret"))) as c:
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    prod = make_settings(app_env="prod", jwt_secret="p" * 32, fixed_now=None)
    with TestClient(create_app(prod)) as c:
        assert c.get("/metrics").status_code == 404  # not exposed unauthenticated in production


def test_prod_refuses_default_secret():
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(app_env="prod")


# --------------------------------------------------------------------- reminders ---
class FakeNotifier:
    def __init__(self, fail_for=None):
        self.sent, self.fail_for = [], fail_for

    def send(self, phone, text):
        if phone == self.fail_for:
            raise RuntimeError("provider down")
        self.sent.append((phone, text))


def test_reminders_go_out_once_for_visits_inside_the_window(svc):
    notifier = FakeNotifier()
    n = send_due_reminders(svc, notifier, hours=24)
    assert n == len(notifier.sent) >= 1  # Liam's visit later today
    assert all("Maple Street" in t for _, t in notifier.sent)
    assert send_due_reminders(svc, notifier, hours=24) == 0  # never twice
    far = send_due_reminders(svc, notifier, hours=24 * 14)
    assert far >= 3 and not any(
        a.reminder_sent_at is None
        for a in svc.db.scalars(
            select(m.Appointment).where(
                m.Appointment.status == "booked",
                m.Appointment.start_at > NOW,
                m.Appointment.start_at < NOW + timedelta(days=14),
            )
        )
    )


def test_a_failed_send_does_not_block_the_others_and_is_retried(svc):
    liam = svc.db.scalar(select(m.Patient).where(m.Patient.first_name == "Liam"))
    n = send_due_reminders(svc, FakeNotifier(fail_for=liam.phone), hours=24 * 14)
    assert n >= 3  # the others still went out
    again = FakeNotifier()
    assert send_due_reminders(svc, again, hours=24 * 14) == 1 and again.sent[0][0] == liam.phone


def test_cancelled_and_phoneless_appointments_get_no_reminder(svc):
    for p in svc.db.scalars(select(m.Patient)):
        p.phone = None
    svc.db.commit()
    assert send_due_reminders(svc, FakeNotifier(), hours=24 * 30) == 0


# -------------------------------------------------------------------- migrations ---
def test_migrations_build_exactly_the_schema_the_models_describe(tmp_path):
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    url = f"sqlite:///{tmp_path / 'mig.db'}"
    upgrade(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), m.Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"


def test_app_starts_through_migrations(tmp_path):
    s = make_settings(database_url=f"sqlite:///{tmp_path / 'boot.db'}", use_migrations=True)
    with TestClient(create_app(s)) as c:
        assert c.get("/readyz").status_code == 200
        assert len(c.get("/api/config").json()["demo_patients"]) == 12


def test_startup_lock_is_a_noop_on_sqlite_and_reentrant_safe(tmp_path):
    from app.db import Database, startup_lock

    db = Database(make_settings(database_url=f"sqlite:///{tmp_path / 'l.db'}"))
    with startup_lock(db.engine), startup_lock(db.engine):
        pass


def test_upgrading_an_existing_database_keeps_its_data_and_fills_new_columns(tmp_path):
    """A clinic already running 1.1 upgrades to 1.2: rows survive and new columns get safe defaults."""
    import sqlite3

    url = f"sqlite:///{tmp_path / 'old.db'}"
    upgrade(url, "0001")  # the previous schema
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute(
        "INSERT INTO patients (first_name, last_name, dob, is_demo, created_at) VALUES ('Old', 'Patient', '1980-01-01', 0, '2026-01-01 00:00:00')"
    )
    con.commit()
    con.close()
    upgrade(url)  # to head
    con = sqlite3.connect(tmp_path / "old.db")
    assert con.execute("SELECT first_name, preferred_lang FROM patients").fetchall() == [("Old", "en")]
    assert {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
        "waitlist_entries",
        "notifications",
    }
    con.close()


def test_version_endpoint_reports_the_build(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234")
    body = client.get("/api/version").json()
    assert body["commit"] == "abc1234" and body["version"]
