"""Property-based tests: whatever a noisy speech recogniser throws at the brain, it must not crash,
corrupt its state, or break its invariants."""

import os
import string
from datetime import date, time

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.brain.entities import (
    extract_datetime,
    extract_name,
    parse_choice,
    parse_dob,
    parse_phone,
)
from app.brain.state import DialogState
from app.brain.text import normalize, yes_no
from tests.conftest import Call

TODAY = date(2026, 10, 26)
FAST = settings(
    max_examples=int(os.environ.get("FUZZ_EXAMPLES", "150")),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Words a real caller (or a bad transcript of one) might produce, mixed with noise.
VOCAB = [
    "book",
    "cancel",
    "reschedule",
    "appointment",
    "flu",
    "shot",
    "physical",
    "yes",
    "no",
    "please",
    "tomorrow",
    "tuesday",
    "next",
    "week",
    "morning",
    "afternoon",
    "at",
    "three",
    "pm",
    "10",
    "30",
    "march",
    "4th",
    "1990",
    "dr",
    "patel",
    "nguyen",
    "kim",
    "any",
    "first",
    "second",
    "last",
    "one",
    "wait",
    "actually",
    "never",
    "mind",
    "human",
    "person",
    "repeat",
    "what",
    "time",
    "open",
    "where",
    "parking",
    "maria",
    "gonzalez",
    "priya",
    "sharma",
    "i'm",
    "my",
    "name",
    "is",
    "born",
    "new",
    "patient",
    "earliest",
    "available",
    "not",
    "instead",
    "sunday",
    "november",
    "26th",
    "noon",
    "half",
    "past",
    "um",
    "uh",
    "hello",
    "thanks",
    "bye",
    "help",
    "555",
    "123",
    "4567",
    "!",
    "?",
    ",",
    ".",
]
utterance = st.one_of(
    st.lists(st.sampled_from(VOCAB), min_size=0, max_size=12).map(" ".join),
    st.text(alphabet=string.printable, max_size=80),
    st.text(max_size=60),  # arbitrary unicode
)


@FAST
@given(utterance)
def test_extractors_never_raise_and_return_sane_values(text):
    n = normalize(text)
    assert n == n.strip() and "  " not in n
    ent = extract_datetime(n, TODAY, allow_bare_day=True, allow_bare_time=True)
    for d in ent.dates:
        assert d.value is None or isinstance(d.value, date)
    for t in ent.times:
        w = t.window
        assert isinstance(w.start, time) and isinstance(w.end, time)
        if w.exact:
            assert w.start <= w.exact <= w.end
    dob = parse_dob(n, TODAY)
    assert dob is None or (dob < TODAY and dob.year >= 1900)
    extract_name(n, expecting=True)
    extract_name(n)
    assert parse_phone(n) is None or len(parse_phone(n)) == 10
    assert yes_no(n) in (None, "yes", "no")
    assert parse_choice(n, 3) in (None, 0, 1, 2)


@FAST
@given(st.integers(1, 12), st.sampled_from([0, 5, 15, 30, 45]), st.sampled_from(["am", "pm"]))
def test_every_clock_time_roundtrips(h, minute, mer):
    text = f"{h}:{minute:02d} {mer}" if minute else f"{h} {mer}"
    got = extract_datetime(normalize(f"at {text}"), TODAY).times[-1].window.exact
    assert got == time(h % 12 + (12 if mer == "pm" else 0), minute)


@FAST
@given(
    st.sampled_from(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]),
    st.sampled_from(["", "next ", "this "]),
)
def test_weekday_phrases_land_in_the_future(day, prefix):
    d = extract_datetime(normalize(prefix + day), TODAY).dates[-1].value
    assert d >= TODAY and d.strftime("%A").lower() == day and (d - TODAY).days <= 14


@FAST
@given(st.lists(utterance, min_size=1, max_size=14))
def test_the_brain_survives_any_conversation(svc, rules, lines):
    call = Call(svc, rules)
    from app.services.engagement import Outbox, WaitlistService
    from tests.test_engagement import Recorder

    svc.waitlist = WaitlistService(svc, Outbox(svc.db, Recorder()))  # exercise the waitlist paths too
    for line in lines:
        reply = call.say(line)
        assert isinstance(reply, str) and reply.strip()
        again = DialogState.from_json(call.state.to_json())  # state must always serialise and reload
        assert again.step == call.state.step and again.slots == call.state.slots
        assert call.state.misses <= 3 and call.state.turn >= 1
        if call.last.ended:
            assert call.state.step == "ENDED"
            break
    # nothing the caller said may leave a half-booked or double-booked database behind
    from sqlalchemy import func, select

    from app import models as m

    svc.db.rollback()
    for appt_id, n in svc.db.execute(
        select(m.AppointmentSlot.appointment_id, func.count()).group_by(m.AppointmentSlot.appointment_id)
    ):
        appt = svc.db.get(m.Appointment, appt_id)
        assert appt is not None and (appt.status == "booked")  # cancelled appointments hold no slots
        assert n == svc.slot_count(appt.service_key)
