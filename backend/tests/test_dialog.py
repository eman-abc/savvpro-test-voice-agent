"""Scripted calls: each test is a messy-caller scenario the task asks us to think about."""

from datetime import datetime

from sqlalchemy import select

from app import models as m


def appts(svc, first, status="booked"):
    """A patient's appointments with this status; "booked" means upcoming (past visits excluded)."""
    p = svc.db.scalar(select(m.Patient).where(m.Patient.first_name == first))
    svc.db.expire_all()
    q = select(m.Appointment).where(m.Appointment.patient_id == p.id, m.Appointment.status == status)
    if status == "booked":
        q = q.where(m.Appointment.start_at > svc.now())
    return list(svc.db.scalars(q.order_by(m.Appointment.start_at)))


def verify(call, who="Wei Zhang, May 17th 1995"):
    call.say(who)


# ------------------------------------------------------------------ happy paths ---
def test_established_patient_books_in_five_turns(call, svc):
    call.say("I need a flu shot")
    assert call.step == "IDENTIFY"
    call.say("Priya Sharma, July 23rd 1990")
    assert call.step == "COLLECT" and call.state.verified
    call.say("any provider")
    reply = call.say("tomorrow at 10 am")
    assert call.step == "CONFIRM" and "10 AM" in reply and "tomorrow" in reply
    reply = call.say("yes")
    assert call.step == "MORE" and "You're booked" in reply and "15 minutes early" in reply
    booked = [a for a in appts(svc, "Priya") if a.start_at == datetime(2026, 10, 27, 10, 0)]
    assert booked and booked[0].service_key == "vaccination"
    assert call.last.appointment["action"] == "booked"


def test_caller_can_give_everything_in_one_sentence(call, svc):
    call.say("book a flu shot with dr patel next tuesday at 3 pm, I'm Wei Zhang born may 17 1995")
    assert call.step == "CONFIRM"
    assert "3 PM" in call.last.reply and "Dr. Patel" in call.last.reply and "Tuesday, November 3rd" in call.last.reply
    call.say("yes")
    assert any(a.start_at == datetime(2026, 11, 3, 15, 0) for a in appts(svc, "Wei"))


def test_faq_does_not_need_verification(call):
    reply = call.say("what time do you open")
    assert "8 AM to 5 PM" in reply and not call.state.verified and call.step == "MORE"


# ---------------------------------------------------------- caller changes mind ---
def test_correction_inside_one_utterance_last_mention_wins(call):
    call.say("book a physical")
    verify(call)
    call.say("any")
    call.say("Thursday at 3, no wait, Friday morning")
    assert call.state.slots["date"] == "2026-10-30"
    assert call.state.slots["time"]["label"] == "morning"


def test_time_changed_at_the_confirmation_step(call, svc):
    call.say("book a flu shot, I'm Wei Zhang born may 17 1995")
    call.say("any")
    call.say("next tuesday at 10")
    assert call.step == "CONFIRM"
    reply = call.say("actually make it 3 pm")
    assert call.step == "CONFIRM" and "3 PM" in reply
    assert any(s["kind"] == "correction" for s in call.trace["steps"])
    call.say("yes")
    assert [a.start_at for a in appts(svc, "Wei")] == [datetime(2026, 11, 3, 15, 0)]


def test_day_changed_while_choosing_between_options(call):
    call.say("book a physical")
    verify(call)
    call.say("any")
    call.say("next tuesday morning")
    assert call.step == "OFFER"
    reply = call.say("how about wednesday instead")
    assert call.state.slots["date"] == "2026-10-28" and "Wednesday" in reply


def test_switching_option_after_confirm_prompt(call):
    call.say("book a physical")
    verify(call)
    call.say("any")
    call.say("next tuesday morning")
    call.say("the second one")
    assert call.step == "CONFIRM"
    call.say("no, the first one")
    assert call.step == "CONFIRM" and call.state.pending["choice"]["start"] == call.state.options[0]["start"]


def test_no_at_confirm_asks_what_to_change_and_clears_the_slot(call):
    call.say("book a flu shot, I'm Wei Zhang born may 17 1995")
    call.say("any")
    call.say("next tuesday at 10")
    reply = call.say("no, change the time")
    assert call.step == "COLLECT" and "What time" in reply and "time" not in call.state.slots


def test_change_of_mind_from_booking_to_cancelling(call):
    call.say("book a physical")
    verify(call, "James Carter, November 2nd 1972")
    call.say("cancel my appointment instead")
    assert call.state.flow == "cancel" and call.step == "CONFIRM"


def test_start_over_keeps_the_verified_patient(call):
    call.say("book a physical")
    verify(call)
    call.say("never mind")
    assert call.step == "ROUTE" and call.state.verified and call.state.flow is None


# ---------------------------------------------------------------- rules in action ---
def test_new_patient_registers_and_is_moved_to_a_new_patient_visit(call, svc):
    call.say("book an appointment")
    call.say("I'm a new patient")
    assert call.step == "REGISTER"
    call.say("Alex Rivera")
    call.say("May 5th 1992")
    reply = call.say("555 123 4567")
    assert call.step == "COLLECT" and "Welcome, Alex" in reply
    reply = call.say("I feel sick")
    assert call.state.slots["service"] == "new_patient_visit" and "40 minutes" in reply
    call.say("any")
    call.say("friday at 9")
    reply = call.say("yes")
    assert "30 minutes early" in reply
    assert svc.db.scalar(select(m.Patient).where(m.Patient.last_name == "Rivera")).phone == "5551234567"


def test_follow_up_requires_a_previous_visit(call):
    call.say("book a follow-up")
    call.say("Aisha Rahman, September 9th 2001")  # a patient with no past visit
    assert call.state.slots["service"] == "new_patient_visit"
    assert any("requires a past visit" in s["detail"] for s in call.trace["steps"])


def test_established_patient_cannot_book_a_new_patient_visit(call):
    call.say("book a new patient visit")
    reply = call.say("Wei Zhang, May 17th 1995")
    assert "only for first-time patients" in reply and "service" not in call.state.slots


def test_provider_must_offer_the_service(call):
    call.say("book a lab test")
    verify(call)
    reply = call.say("Dr. Patel")
    assert "doesn't offer" in reply and "NP Kim" in reply and call.state.slots.get("provider") != "patel"


def test_closed_day_and_holiday_are_explained(call):
    call.say("book a physical")
    verify(call)
    call.say("any")
    assert "closed on Sundays" in call.say("sunday")
    assert "holiday" in call.say("november 26th")
    assert call.step == "COLLECT" and "date" not in call.state.slots


def test_provider_day_off_is_explained(call):
    call.say("book a physical")
    verify(call)
    reply = call.say("Dr. Nguyen on wednesday")
    assert "doesn't work on Wednesdays" in reply


def test_exact_time_taken_offers_closest_alternatives(call, svc):
    call.say("book an annual physical thursday at 10")
    verify(call)
    call.say("Dr Patel")  # Maria's follow-up already holds Thursday 10:00 with Dr. Patel
    assert call.step == "OFFER" and "10 AM isn't available" in call.last.reply
    assert len(call.state.options) == 3


def test_yes_to_several_options_asks_which(call):
    call.say("book an annual physical thursday at 10")
    verify(call)
    call.say("Dr Patel")
    reply = call.say("yes")
    assert "Which one" in reply and call.step == "OFFER"


def test_earliest_available(call):
    call.say("book a flu shot")
    verify(call)
    call.say("any")
    reply = call.say("earliest available")
    assert call.step == "OFFER" and "earliest openings" in reply and len(call.state.options) == 3


def test_slot_taken_between_offer_and_confirm(call, svc):
    """Someone else books the slot while our caller is deciding: nothing is double-booked."""
    call.say("book a flu shot, I'm Wei Zhang born may 17 1995")
    call.say("dr patel")
    call.say("next tuesday at 10")
    assert call.step == "CONFIRM"
    other = svc.db.scalar(select(m.Patient).where(m.Patient.first_name == "Jose"))
    svc.book(other.id, "patel", "vaccination", datetime(2026, 11, 3, 10, 0), actor="someone-else")
    reply = call.say("yes")
    assert "just taken" in reply and call.step == "OFFER"
    assert appts(svc, "Wei", "booked") == []


def test_patient_double_booking_themselves_is_refused(call, svc):
    wei = svc.db.scalar(select(m.Patient).where(m.Patient.first_name == "Wei"))
    svc.book(wei.id, "nguyen", "general_consultation", datetime(2026, 11, 3, 10, 0), actor="t")
    call.say("book a flu shot, I'm Wei Zhang born may 17 1995")
    call.say("dr patel")
    call.say("next tuesday at 10")
    reply = call.say("yes")
    assert "already have an appointment at that time" in reply


# ------------------------------------------------------------ reschedule / cancel ---
def test_late_cancel_warns_then_flags_the_appointment(call, svc):
    call.say("I need to cancel my appointment")
    reply = call.say("Liam O'Connor, January 30th 1968")
    assert "late cancellation" in reply and call.step == "CONFIRM"
    reply = call.say("yes")
    assert "fee may apply" in reply
    cancelled = appts(svc, "Liam", "cancelled")
    assert cancelled and cancelled[0].late_change is True


def test_cancel_declined_keeps_the_appointment(call, svc):
    call.say("cancel my appointment")
    call.say("James Carter, November 2nd 1972")
    call.say("no")
    assert len(appts(svc, "James")) == 1 and call.step == "MORE"


def test_reschedule_with_two_appointments_asks_which(call, svc):
    call.say("reschedule my appointment")
    reply = call.say("Olivia Johnson-Smith, December 5th 1979")
    assert call.step == "SELECT" and "The first is" in reply and "The second is" in reply
    call.say("the lab one")
    assert call.state.slots["service"] == "lab_work" and call.state.slots["provider"] == "kim"
    call.say("next tuesday afternoon")
    call.say("the last one")
    reply = call.say("yes")
    assert "Done" in reply
    lab = [a for a in appts(svc, "Olivia") if a.service_key == "lab_work"][0]
    assert lab.reschedule_count == 1 and lab.start_at.hour >= 13


def test_late_reschedule_requires_acknowledging_the_fee_rule(call):
    call.say("move my appointment")
    reply = call.say("Liam O'Connor, January 30th 1968")
    assert "late cancellation" in reply and call.state.pending["kind"] == "late_continue"
    call.say("no")
    assert call.step == "MORE"


def test_no_upcoming_appointments_offers_to_book(call):
    call.say("cancel my appointment")
    reply = call.say("Wei Zhang, May 17th 1995")
    assert "don't see any upcoming appointments" in reply and call.state.pending.get("offer") == "book"
    call.say("yes")
    assert call.state.flow == "book"


def test_list_appointments(call):
    call.say("when is my next appointment")
    reply = call.say("James Carter, November 2nd 1972")
    assert "annual physical with Dr. Nguyen" in reply and "Monday, November 2nd" in reply


def test_cant_make_it_is_clarified_not_guessed(call):
    reply = call.say("I can't make it to my appointment")
    assert call.step == "CLARIFY" and "reschedule" in reply and "cancel" in reply
    call.say("cancel it")
    assert call.state.flow == "cancel"


# -------------------------------------------------------------- safety & handoff ---
def test_emergency_ends_the_call_immediately(call):
    reply = call.say("I have chest pain and need to book")
    assert "911" in reply and call.last.ended and call.state.outcome() == "emergency"


def test_wrong_identity_three_times_hands_off_without_revealing_which_part(call):
    call.say("book an appointment")
    r1 = call.say("John Smith, January 1st 1980")
    assert "couldn't match those details" in r1 and "date of birth was" not in r1
    call.say("Jane Doe, March 3rd 1975")
    reply = call.say("Foo Bar, June 6th 1966")
    assert call.last.ended and call.state.end_reason == "handoff" and "transferring you" in reply


def test_new_patient_hint_appears_after_second_failure(call):
    call.say("book an appointment")
    call.say("John Smith, January 1st 1980")
    reply = call.say("Jane Doe, March 3rd 1975")
    assert "new patient" in reply


def test_asking_for_a_human_hands_off_with_a_summary(call):
    call.say("book a physical")
    verify(call)
    call.say("let me talk to a real person")
    assert call.last.ended and "verified patient: yes" in call.state.handoff_summary
    assert "book" in call.state.handoff_summary


def test_frustration_hands_off(call):
    call.say("this is useless")
    assert call.state.end_reason == "handoff"


def test_gibberish_escalates_after_three_misses(call):
    call.say("blorp")
    assert call.state.misses == 1 and not call.last.ended
    call.say("asdf qwer")
    assert not call.last.ended
    call.say("zxcv bnm")
    assert call.last.ended and call.state.end_reason == "handoff"


def test_silence_prompts_once_then_hangs_up(call):
    reply = call.say("")
    assert "still there" in reply and not call.last.ended and call.state.misses == 0
    reply = call.say("")
    assert call.last.ended and call.state.end_reason == "silence" and call.state.outcome() == "no_response"
    assert "end the call" in reply


def test_speaking_after_a_silence_prompt_resets_the_count(call):
    call.say("")
    call.say("what time do you open")
    assert call.state.silences == 0
    call.say("")
    assert not call.last.ended  # only one silence since the caller spoke


def test_misses_reset_after_a_good_turn(call):
    call.say("blorp")
    call.say("what time do you open")
    assert call.state.misses == 0


def test_faq_mid_flow_answers_then_returns_to_the_same_step(call):
    call.say("book a lab test")
    verify(call)
    reply = call.say("what do I need to bring?")
    assert "photo ID" in reply and "What day would you like" in reply and call.step == "COLLECT"


def test_a_weekday_answer_is_not_mistaken_for_the_hours_faq_mid_flow(call):
    call.say("book a physical")
    verify(call)
    call.say("any")
    call.say("saturday")  # 'saturday' is a keyword of the hours FAQ, but here it answers the date question
    assert call.state.slots["date"] == "2026-10-31"


def test_repeat_replays_the_last_reply_without_moving_state(call):
    first = call.say("book a physical")
    again = call.say("can you repeat that")
    assert again == first and call.step == "IDENTIFY"


def test_call_after_goodbye_is_closed(call):
    call.say("goodbye")
    assert call.last.ended
    assert "ended" in call.say("hello?")


def test_trace_explains_the_decision(call):
    call.say("book a physical")
    t = call.trace
    assert t["intent"]["name"] == "book" and t["intent"]["decision"] == "act"
    assert t["state"] == {"from": "ROUTE", "to": "IDENTIFY", "flow": "book"}
    assert t["steps"] and t["reply"]
