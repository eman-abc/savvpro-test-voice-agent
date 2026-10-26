import pytest

from app.brain.intents import classify, decide
from app.brain.retrieval import FaqIndex
from app.brain.text import normalize


@pytest.fixture
def faq(rules):
    return FaqIndex(rules.faq)


def decision(faq, text):
    n = normalize(text)
    hit = faq.search(n)
    res = classify(n, hit.score if hit else 0.0)
    return decide(res), hit


@pytest.mark.parametrize(
    "text,intent",
    [
        ("I'd like to book an appointment", "book"),
        ("I want to see a doctor", "book"),
        ("I need a flu shot", "book"),
        ("I need to cancel my appointment", "cancel"),
        ("I want to cancel", "cancel"),
        ("can you reschedule my visit", "reschedule"),
        ("move my appointment to thursday", "reschedule"),
        ("when is my next appointment", "list"),
        ("let me talk to a real person", "handoff"),
        ("this is useless", "handoff"),
        ("what time do you open", "faq"),
        ("is there parking", "faq"),
        ("what's your cancellation policy", "faq"),
        ("how early should I arrive", "faq"),
        ("hello", "greeting"),
        ("can you repeat that", "repeat"),
        ("start over", "startover"),
        ("goodbye", "goodbye"),
    ],
)
def test_clear_intents(faq, text, intent):
    d, _ = decision(faq, text)
    assert (d.kind, d.intent) == ("act", intent)


def test_cant_make_it_is_ambiguous_not_guessed(faq):
    d, _ = decision(faq, "I can't make it to my appointment")
    assert d.kind == "clarify" and {d.intent, d.alt} == {"reschedule", "cancel"}


def test_cancellation_policy_question_is_not_a_cancel_request(faq):
    d, hit = decision(faq, "what is your cancellation policy")
    assert d.intent == "faq" and hit.entry.id == "cancellation"


def test_unknown_input_is_none(faq):
    assert decision(faq, "blorp")[0].kind == "none"


@pytest.mark.parametrize(
    "text,faq_id",
    [("where are you located", "location"), ("do you accept insurance", "insurance"), ("what should I bring", "bring")],
)
def test_faq_retrieval(faq, text, faq_id):
    assert faq.search(normalize(text)).entry.id == faq_id
