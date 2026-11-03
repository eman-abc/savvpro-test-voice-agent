"""When speech recognition is unsure, the assistant reads back what it heard before acting. Rules decide; the model only reports."""

from tests.conftest import Call

LOW, HIGH = 0.3, 0.92


def test_low_confidence_free_form_speech_is_read_back_first(call):
    reply = call.say("book a flu shot please", asr=LOW)
    assert call.step == "HEARD" and "I heard: “book a flu shot please”" in reply
    assert any(s["label"] == "Low speech confidence" for s in call.trace["steps"])


def test_yes_then_processes_the_original_words(call):
    call.say("book a flu shot please", asr=LOW)
    call.say("yes")
    assert call.step == "IDENTIFY" and call.state.flow == "book" and call.state.slots["service"] == "vaccination"


def test_no_asks_again_and_returns_to_the_same_step(call):
    call.say("book a flu shot please", asr=LOW)
    reply = call.say("no")
    assert call.step == "ROUTE" and "say it again" in reply and call.state.flow is None


def test_saying_something_new_is_treated_as_the_answer(call):
    call.say("book a flu shot please", asr=LOW)
    call.say("actually cancel my appointment")
    assert call.state.flow == "cancel" and call.step == "IDENTIFY"


def test_typed_text_and_confident_speech_are_never_read_back(call):
    call.say("book a flu shot please")  # typed: no confidence number
    assert call.step == "IDENTIFY"
    c2 = Call(call.svc, call.brain.rules)
    c2.say("book a flu shot please", asr=HIGH)
    assert c2.step == "IDENTIFY"


def test_the_previous_step_and_pending_state_are_restored(call):
    call.say("book a physical")
    assert call.step == "IDENTIFY"
    call.say("Wei Zhang May 17th 1995", asr=LOW)
    assert call.step == "HEARD" and call.state.pending["return_step"] == "IDENTIFY"
    call.say("yes")
    assert call.state.verified and call.step == "COLLECT" and call.state.pending == {}


def test_yes_no_steps_and_single_words_are_not_read_back(call):
    call.say("book a flu shot, I'm Wei Zhang born may 17 1995")
    call.say("any provider")
    call.say("next tuesday at 2 pm")
    assert call.step == "CONFIRM"
    call.say("yes", asr=LOW)  # a confirmation is a yes/no: never read back
    assert call.step == "MORE"
    c2 = Call(call.svc, call.brain.rules)
    c2.say("book a physical")
    c2.say("Wei Zhang May 17th 1995")
    c2.say("tomorrow", asr=LOW)  # one word: acting on it is cheap, and a read-back would just annoy
    assert c2.step != "HEARD"


def test_emergency_words_are_acted_on_even_when_recognition_is_unsure(call):
    call.say("I have chest pain", asr=0.05)
    assert call.last.ended and call.state.outcome() == "emergency"


def test_urdu_read_back_uses_urdu_and_the_callers_own_words(svc, rules):
    c = Call(svc, rules)
    c.state.lang = "ur"
    c.last = c.brain.start(c.state)
    reply = c.say("مجھے فلو کا ٹیکہ چاہیے", asr=LOW)
    assert c.step == "HEARD" and "میں نے سنا" in reply and "مجھے فلو کا ٹیکہ چاہیے" in reply
    c.say("جی ہاں")
    assert c.step == "IDENTIFY" and c.state.flow == "book"


def test_a_read_back_repeats_itself_if_asked(call):
    first = call.say("book a flu shot please", asr=LOW)
    assert call.say("can you repeat that") == first and call.step == "HEARD"


def test_over_http_the_confidence_is_validated_and_used(client):
    cid = client.post("/api/calls").json()["call_id"]
    bad = client.post(f"/api/calls/{cid}/turn", json={"text": "book a flu shot please", "asr_confidence": 1.5})
    assert bad.status_code == 422
    ok = client.post(f"/api/calls/{cid}/turn", json={"text": "book a flu shot please", "asr_confidence": 0.2}).json()
    assert "I heard" in ok["reply"] and ok["trace"]["state"]["to"] == "HEARD"
