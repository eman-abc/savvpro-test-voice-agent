"""Emergency recall. A missed emergency is the worst failure this assistant can have, so the matcher is tested on many phrasings
(English and Urdu, any word order), for false alarms, and for agreement between the rules engine and the LLM agent."""

import pytest

from app.agent.brain import AgentBrain
from app.agent.testing import ScriptedChatModel, say
from app.brain.safety import emergency_match
from app.brain.state import DialogState
from tests.conftest import Call

MUST_ALARM_EN = [
    "I have chest pain",
    "I have severe chest pain",
    "pain in my chest",
    "there is a lot of pain in my chest",
    "my chest is tight and it hurts",
    "I can't breathe",
    "he is not breathing",
    "she stopped breathing",
    "my dad passed out",
    "my son collapsed",
    "I think it's a heart attack",
    "he is unconscious",
    "she is choking",
    "he is having a seizure",
    "I took an overdose",
    "this is an emergency",
]
MUST_ALARM_UR = [
    "سینے میں درد ہے",
    "سینے میں شدید درد ہے",
    "مجھے سینے میں بہت تیز درد ہو رہا ہے",
    "مجھے سانس نہیں آ رہی",
    "سانس لینے میں مشکل ہے",
    "میرا بچہ بے ہوش ہو گیا ہے",
    "دل کا دورہ پڑا ہے",
    "ہنگامی صورتحال ہے",
    "اسے فالج ہو گیا ہے",
    "بہت زیادہ خون بہہ رہا ہے",
]
MUST_NOT_ALARM = [
    "I want to cancel my chest x-ray",
    "do you give flu shots",
    "what is your parking like",
    "I need to book a physical",
    "my appointment is tight on time",
    "ہفتے کو کلینک کھلتا ہے",
    "ٹیکہ لگوانا ہے",
    "پارکنگ کہاں ہے",
    "اپائنٹمنٹ منسوخ کرنی ہے",
    "کلینک کا پتہ بتائیں",
]


def alarmed_by_rules(svc, rules, text):
    c = Call(svc, rules)
    c.say(text)
    return c.state.end_reason == "emergency"


@pytest.mark.parametrize("text", MUST_ALARM_EN + MUST_ALARM_UR)
def test_the_rules_engine_raises_the_alarm(svc, rules, text):
    assert alarmed_by_rules(svc, rules, text), text


@pytest.mark.parametrize("text", MUST_NOT_ALARM)
def test_ordinary_requests_do_not_raise_a_false_alarm(svc, rules, text):
    assert not alarmed_by_rules(svc, rules, text), text


@pytest.mark.parametrize("text", MUST_ALARM_EN + MUST_ALARM_UR + MUST_NOT_ALARM)
def test_the_llm_agent_and_the_rules_engine_always_agree(svc, rules, text):
    """The agent's guard runs before the model. If it ever disagreed with the rules engine, switching brains would change safety."""
    from app.knowledge.docs import build_documents
    from app.knowledge.graph import InMemoryGraph, build_graph_spec
    from app.knowledge.rag import KnowledgeService
    from app.knowledge.search import Bm25Search

    docs = build_documents(rules)
    llm = ScriptedChatModel(script=[say("ok")])
    brain = AgentBrain(
        svc, KnowledgeService(docs, Bm25Search(docs), InMemoryGraph(build_graph_spec(rules, docs))), llm, "t"
    )
    st = DialogState()
    brain.handle(st, text)
    agent_alarmed = st.end_reason == "emergency"
    assert agent_alarmed == alarmed_by_rules(svc, rules, text), text
    assert (llm.prompts == []) == agent_alarmed  # an emergency never reaches the model; anything else does


def test_words_far_apart_are_not_an_emergency():
    assert (
        emergency_match("my chest of drawers arrived and there was no pain at all in my wrist", ["chest pain"]) is None
    )
    assert emergency_match("pain in my chest", ["chest pain"]) == "chest pain"
    assert emergency_match("chest x ray", ["chest pain"]) is None


def test_single_word_keywords_still_match_by_prefix():
    assert emergency_match("i feel suicidal", ["suicid"]) == "suicid"
    assert emergency_match("call 911 now", ["911"]) == "911"
