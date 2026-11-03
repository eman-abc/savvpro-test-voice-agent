"""The LangGraph agent, driven by a scripted model so the REAL graph, tools, gates and database run with no network.

The point of these tests is the safety design: the model plans and phrases, but deterministic code owns identity, confirmation,
emergencies and every write. Several tests make the "model" misbehave on purpose and check the code refuses."""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import models as m
from app.agent.brain import AgentBrain
from app.agent.llm import PRESETS, make_chat_model
from app.agent.testing import ScriptedChatModel, call, say
from app.brain.dialog import Brain
from app.brain.retrieval import FaqIndex
from app.brain.state import DialogState
from app.knowledge.docs import build_documents
from app.knowledge.graph import InMemoryGraph, build_graph_spec
from app.knowledge.rag import KnowledgeService
from app.knowledge.search import Bm25Search
from app.main import create_app
from tests.conftest import make_settings

WEI = dict(full_name="Wei Zhang", date_of_birth="1995-05-17")


@pytest.fixture
def knowledge(rules):
    docs = build_documents(rules)
    return KnowledgeService(docs, Bm25Search(docs), InMemoryGraph(build_graph_spec(rules, docs)))


def make(svc, knowledge, rules, script, *, fallback=True, max_steps=6):
    llm = ScriptedChatModel(script=list(script))
    rules_brain = Brain(svc, FaqIndex(rules.faq), "t") if fallback else None
    return AgentBrain(svc, knowledge, llm, "agent:test", rules_brain, max_steps=max_steps), llm, DialogState()


def tool_outputs(prompt):
    """The JSON the model was shown as tool results in its latest prompt."""
    return [json.loads(x.content) for x in prompt if x.type == "tool"]


def a_real_slot(svc, service="vaccination", provider="patel"):
    return svc.slots_for_day(date(2026, 10, 27), service, provider)[0].start.isoformat()


def stage_booking_script(svc):
    return [
        call("verify_patient", **WEI),
        call("propose_booking", service="vaccination", provider="patel", start=a_real_slot(svc)),
        say("Shall I book that?"),
    ]


# ------------------------------------------------------------------------------------------------ happy paths
def test_a_faq_is_answered_from_the_knowledge_tool_with_a_trace(svc, knowledge, rules):
    brain, llm, st = make(
        svc,
        knowledge,
        rules,
        [call("search_knowledge", question="opening hours"), say("We are open Monday to Friday, 8 to 5.")],
    )
    r = brain.handle(st, "what time do you open")
    shown = tool_outputs(llm.prompts[-1])[0]
    assert shown["ok"] and shown["results"][0]["source"].startswith(
        "clinic.yaml"
    )  # the model saw the facts and where they came from
    assert r.reply.startswith("We are open") and "faq" in st.actions
    assert [s["label"] for s in r.trace["steps"]][:1] == ["Tool search_knowledge"]
    assert not r.ended


def test_a_booking_needs_a_proposal_then_a_yes_in_a_later_turn(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, stage_booking_script(svc))
    r1 = brain.handle(st, "I need a flu shot tomorrow, I'm Wei Zhang born 17 May 1995")
    assert st.verified and st.pending["action"] == "book" and r1.reply == "Shall I book that?"
    assert svc.upcoming(st.patient_id) == []  # nothing is written by a proposal
    llm.script += [call("confirm_pending"), say("You're booked.")]
    r2 = brain.handle(st, "yes please")
    [appt] = svc.upcoming(st.patient_id)
    assert (appt.provider.key, appt.service_key, appt.created_via) == ("patel", "vaccination", "agent")
    assert r2.appointment["action"] == "booked" and r2.appointment["id"] == appt.id
    assert st.outcome() == "booked" and st.pending == {} and r2.progress["current"] == 4


def test_the_model_sees_authoritative_state_in_its_system_prompt(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, [*stage_booking_script(svc), say("ok")])
    brain.handle(st, "hello")
    brain.handle(st, "never mind")
    system = llm.prompts[-1][0].content
    assert (
        "verified patient, first name Wei" in system
        and "Staged action awaiting confirmation: vaccination visit" in system
    )
    assert "today is Monday, October 26, 2026" in system


def test_conversation_memory_survives_a_json_round_trip(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, [say("Which day suits you?"), say("Tuesday works.")])
    brain.handle(st, "I'd like an appointment")
    st = DialogState.from_json(st.to_json())  # what the database does between turns
    brain.handle(st, "Tuesday")
    texts = [x.content for x in llm.prompts[-1] if x.type in ("human", "ai")]
    assert texts == ["I'd like an appointment", "Which day suits you?", "Tuesday"]


def test_urdu_input_switches_the_reply_language(svc, knowledge, rules):
    brain, _, st = make(svc, knowledge, rules, [say("جی، میں مدد کر سکتا ہوں۔")])
    brain.handle(st, "مجھے اپائنٹمنٹ چاہیے")
    assert st.lang == "ur"


def test_silence_asks_once_then_hangs_up_without_calling_the_model(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, [])
    assert brain.handle(st, "").reply == "Are you still there?"
    r = brain.handle(st, "")
    assert r.ended and st.outcome() == "no_response" and llm.prompts == []


# ------------------------------------------------------------------------------------------------ the gates (the model misbehaves)
def test_no_appointment_work_before_identity_is_verified(svc, knowledge, rules):
    brain, llm, st = make(
        svc,
        knowledge,
        rules,
        [
            call("propose_booking", service="vaccination", provider="patel", start=a_real_slot(svc)),
            say("Sorry, who are you?"),
        ],
    )
    r = brain.handle(st, "book me in")
    assert tool_outputs(llm.prompts[-1])[0]["error"] == "not_verified"
    assert st.pending == {} and not st.verified
    assert any(
        s["kind"] == "rule" and "refused" in s["label"] for s in r.trace["steps"]
    )  # the refusal is visible in the trace


def test_the_model_cannot_confirm_in_the_same_turn_it_proposes(svc, knowledge, rules):
    script = [
        call("verify_patient", **WEI),
        call("propose_booking", service="vaccination", provider="patel", start=a_real_slot(svc)),
        call("confirm_pending"),
        say("All done!"),
    ]  # the model lies: it claims success
    brain, llm, st = make(svc, knowledge, rules, script)
    brain.handle(st, "book a flu shot, Wei Zhang 17 May 1995")
    assert tool_outputs(llm.prompts[-1])[-1]["error"] == "needs_caller_confirmation"
    assert svc.upcoming(st.patient_id) == [] and st.pending["action"] == "book" and "booked" not in st.actions


def test_a_no_from_the_caller_blocks_confirmation(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, stage_booking_script(svc))
    brain.handle(st, "flu shot, Wei Zhang 17 May 1995")
    llm.script += [call("confirm_pending"), say("Okay, I won't.")]
    brain.handle(st, "no, not that time")
    assert tool_outputs(llm.prompts[-1])[0]["error"] == "caller_did_not_confirm"
    assert svc.upcoming(st.patient_id) == []


def test_an_ambiguous_reply_is_not_a_yes(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, stage_booking_script(svc))
    brain.handle(st, "flu shot, Wei Zhang 17 May 1995")
    llm.script += [call("confirm_pending"), say("Could you confirm?")]
    brain.handle(st, "hmm what about the afternoon")
    assert tool_outputs(llm.prompts[-1])[0]["error"] == "caller_did_not_confirm" and svc.upcoming(st.patient_id) == []


def test_a_slot_taken_between_proposal_and_confirmation_is_handled(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, stage_booking_script(svc))
    brain.handle(st, "flu shot, Wei Zhang 17 May 1995")
    other, _ = svc.find_patient("Maria Gonzalez", date(1985, 3, 14))
    svc.book(
        other.id, "patel", "vaccination", __import__("datetime").datetime.fromisoformat(st.pending["start"]), "test"
    )  # someone else gets there first
    llm.script += [call("confirm_pending"), say("Sorry, that was just taken.")]
    brain.handle(st, "yes")
    assert (
        tool_outputs(llm.prompts[-1])[0]["error"] == "slot_just_taken"
        and st.pending == {}
        and svc.upcoming(st.patient_id) == []
    )


def test_wrong_details_never_reveal_which_part_failed_and_repeated_failures_hand_over(svc, knowledge, rules):
    wrong = dict(full_name="Wei Zhang", date_of_birth="1990-01-01")
    brain, llm, st = make(
        svc, knowledge, rules, [call("verify_patient", **wrong), say("That didn't match, try again?")]
    )
    brain.handle(st, "I'm Wei Zhang")
    out = tool_outputs(llm.prompts[-1])[0]
    assert out["error"] == "not_verified" and "Do not say which part" in out["hint"] and not st.verified
    llm.script += [
        call("verify_patient", **wrong),
        say("Again?"),
        call("verify_patient", **wrong),
        say("Transferring you."),
    ]
    brain.handle(st, "same again")
    r = brain.handle(st, "and again")  # the policy allows three attempts
    assert r.ended and st.outcome() == "handoff"


def test_a_new_patient_cannot_register_over_an_existing_record(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, [call("register_patient", **WEI), say("I'll pass you to staff.")])
    r = brain.handle(st, "I'm a new patient called Wei Zhang born 17 May 1995")
    assert tool_outputs(llm.prompts[-1])[0]["error"] == "cannot_register"
    assert not st.verified and st.patient_id is None and r.ended and st.outcome() == "handoff"


def test_a_late_cancellation_warns_about_the_fee_before_asking(svc, knowledge, rules):
    liam, _ = svc.find_patient("Liam O'Connor", date(1968, 1, 30))
    appt = svc.upcoming(liam.id)[0]
    script = [
        call("verify_patient", full_name="Liam O'Connor", date_of_birth="1968-01-30"),
        call("propose_cancel", appointment_id=appt.id),
        say("A fee may apply. Cancel anyway?"),
    ]
    brain, llm, st = make(svc, knowledge, rules, script)
    brain.handle(st, "cancel my appointment, Liam O'Connor born 30 January 1968")
    staged = tool_outputs(llm.prompts[-1])[-1]
    assert staged["late_change"] is True and "late-change fee" in staged["fee_warning"]
    llm.script += [call("confirm_pending"), say("Cancelled.")]
    r = brain.handle(st, "yes go ahead")
    assert st.outcome() == "cancelled" and r.appointment["action"] == "cancelled"
    assert svc.get_appointment(appt.id).status == "cancelled" and svc.get_appointment(appt.id).late_change


def test_a_patient_cannot_cancel_somebody_elses_appointment(svc, knowledge, rules):
    liam, _ = svc.find_patient("Liam O'Connor", date(1968, 1, 30))
    theirs = svc.upcoming(liam.id)[0].id
    brain, llm, st = make(
        svc,
        knowledge,
        rules,
        [call("verify_patient", **WEI), call("propose_cancel", appointment_id=theirs), say("I can't find that one.")],
    )
    brain.handle(st, "Wei Zhang 17 May 1995, cancel appointment")
    assert tool_outputs(llm.prompts[-1])[-1]["error"] == "appointment_not_found" and st.pending == {}


def test_a_service_for_the_other_kind_of_patient_is_refused(svc, knowledge, rules):
    slot = svc.slots_for_day(date(2026, 10, 27), "new_patient_visit", "patel")[0].start.isoformat()
    brain, llm, st = make(
        svc,
        knowledge,
        rules,
        [
            call("verify_patient", **WEI),
            call("propose_booking", service="new_patient_visit", provider="patel", start=slot),
            say("That one is for new patients."),
        ],
    )
    brain.handle(st, "Wei Zhang 17 May 1995, new patient visit please")
    assert tool_outputs(llm.prompts[-1])[-1]["error"] == "service_not_available_to_this_patient"


# ------------------------------------------------------------------------------------------------ deterministic guards around the model
def test_an_emergency_never_reaches_the_model(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, [])
    r = brain.handle(st, "I have chest pain")
    assert r.ended and st.outcome() == "emergency" and "911" in r.reply and llm.prompts == []


def test_an_urdu_emergency_is_caught_too(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, [])
    r = brain.handle(st, "سینے میں شدید درد ہے")
    assert r.ended and st.outcome() == "emergency" and "1122" in r.reply and llm.prompts == []


def test_a_runaway_model_is_stopped_and_the_caller_handed_to_staff(svc, knowledge, rules):
    brain, llm, st = make(svc, knowledge, rules, [call("list_services") for _ in range(10)], max_steps=3)
    r = brain.handle(st, "hello")
    assert r.ended and st.outcome() == "handoff" and "team" in r.reply
    assert len(llm.prompts) == 3 and any(s["label"] == "Step limit reached" for s in r.trace["steps"])


def test_if_the_model_fails_the_rules_engine_answers_instead(svc, knowledge, rules):
    brain, _, st = make(
        svc, knowledge, rules, []
    )  # an empty script makes the scripted model raise, like a provider outage
    brain.rules_brain.start(st)
    r = brain.handle(st, "what are your opening hours")
    assert "8 AM" in r.reply and st.turn == 1  # the failed model attempt did not use up a turn number
    assert r.trace["steps"][0]["label"] == "Language model unavailable"


def test_without_a_fallback_a_model_outage_gets_an_apology_not_a_crash(svc, knowledge, rules):
    brain, _, st = make(svc, knowledge, rules, [], fallback=False)
    r = brain.handle(st, "hello")
    assert "trouble" in r.reply and not r.ended


# ------------------------------------------------------------------------------------------------ individual tools
def run_tool(svc, knowledge, rules, script, text="x"):
    brain, llm, st = make(svc, knowledge, rules, [*script, say("ok")])
    brain.handle(st, text)
    return tool_outputs(llm.prompts[-1]), st


def test_availability_validates_its_inputs(svc, knowledge, rules):
    outs, _ = run_tool(
        svc,
        knowledge,
        rules,
        [
            call("find_availability", service="teleportation"),
            call("find_availability", service="lab_work", provider="patel"),
            call("find_availability", service="vaccination", date="next tuesday"),
            call("find_availability", service="vaccination", date="2026-11-26"),
        ],
    )
    assert [o["error"] for o in outs] == [
        "unknown_service",
        "provider_does_not_offer_service",
        "bad_date",
        "unavailable_day",
    ]  # the last is a holiday


def test_availability_offers_real_bookable_times_and_filters_by_part_of_day(svc, knowledge, rules):
    outs, _ = run_tool(
        svc,
        knowledge,
        rules,
        [call("find_availability", service="vaccination", date="2026-10-27", part_of_day="afternoon")],
    )
    opts = outs[0]["options"]
    assert (
        opts
        and all(int(o["start"][11:13]) >= 12 for o in opts)
        and all(o["provider"] in ("patel", "kim") for o in opts)
    )
    from datetime import datetime

    assert all(
        datetime.fromisoformat(o["start"]) in svc.starts_for_day(o["provider"], date(2026, 10, 27), "vaccination")
        for o in opts
    )  # the database agrees


def test_list_services_matches_the_rules(svc, knowledge, rules):
    outs, _ = run_tool(svc, knowledge, rules, [call("list_services")])
    assert {s["key"] for s in outs[0]["services"]} == {s.key for s in rules.services}


def test_my_appointments_requires_verification(svc, knowledge, rules):
    outs, _ = run_tool(svc, knowledge, rules, [call("list_my_appointments")])
    assert outs[0]["error"] == "not_verified"


def test_handoff_and_end_call_end_the_call(svc, knowledge, rules):
    _, st = run_tool(svc, knowledge, rules, [call("handoff_to_staff", reason="caller is frustrated")])
    assert st.ended and st.outcome() == "handoff" and st.handoff_summary == "caller is frustrated"
    _, st = run_tool(svc, knowledge, rules, [call("end_call")])
    assert st.ended and st.end_reason == "goodbye"


# ------------------------------------------------------------------------------------------------ through the real API
def test_the_agent_brain_serves_real_calls_over_http(rules):
    llm = ScriptedChatModel(script=[say("We open at eight.")])
    with TestClient(create_app(make_settings(brain="agent"), llm=llm)) as c:
        start = c.post("/api/calls").json()
        assert "automated assistant" in start["reply"]  # the greeting still comes from the rules engine
        out = c.post(f"/api/calls/{start['call_id']}/turn", json={"text": "when do you open", "seq": 1}).json()
        assert out["reply"] == "We open at eight." and out["trace"]["state"]["to"] == "AGENT"
        detail = c.post("/api/auth/login", json={"username": "reception", "password": "test-pass-123"})
        assert detail.status_code == 200


def test_http_retries_are_still_idempotent_with_the_agent(rules):
    llm = ScriptedChatModel(script=[say("First answer."), say("Should never be used.")])
    with TestClient(create_app(make_settings(brain="agent"), llm=llm)) as c:
        cid = c.post("/api/calls").json()["call_id"]
        first = c.post(f"/api/calls/{cid}/turn", json={"text": "hello", "seq": 1}).json()
        again = c.post(
            f"/api/calls/{cid}/turn", json={"text": "hello", "seq": 1}
        ).json()  # the response was lost; the client retries
        assert first["reply"] == again["reply"] == "First answer." and len(llm.script) == 1


def test_brain_agent_without_a_model_quietly_uses_the_rules_engine():
    with TestClient(create_app(make_settings(brain="agent", llm_provider="none"))) as c:
        cid = c.post("/api/calls").json()["call_id"]
        out = c.post(f"/api/calls/{cid}/turn", json={"text": "what are your opening hours", "seq": 1}).json()
        assert "8 AM" in out["reply"] and out["trace"]["state"]["to"] != "AGENT"


def test_a_model_outage_over_http_falls_back_to_the_rules(rules):
    with TestClient(create_app(make_settings(brain="agent"), llm=ScriptedChatModel(script=[]))) as c:
        cid = c.post("/api/calls").json()["call_id"]
        out = c.post(f"/api/calls/{cid}/turn", json={"text": "what are your opening hours", "seq": 1}).json()
        assert "8 AM" in out["reply"]
        assert out["trace"]["steps"][0]["label"] == "Language model unavailable"


def test_the_agents_memory_is_stored_with_the_call(rules):
    app = create_app(make_settings(brain="agent"), llm=ScriptedChatModel(script=[say("Hello there.")]))
    with TestClient(app) as c:
        cid = c.post("/api/calls").json()["call_id"]
        c.post(f"/api/calls/{cid}/turn", json={"text": "hi", "seq": 1})
        with app.state.ctx.db.session() as s:
            st = DialogState.from_json(s.get(m.CallSession, cid).state_json)
        assert st.agent["messages"] == [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "Hello there."}]


# ------------------------------------------------------------------------------------------------ the model factory
def test_no_provider_means_no_model():
    assert make_chat_model(make_settings(llm_provider="none")) is None


def test_bad_provider_or_missing_model_is_rejected():
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        make_chat_model(make_settings(llm_provider="mystery", llm_model="x"))
    with pytest.raises(ValueError, match="LLM_MODEL"):
        make_chat_model(make_settings(llm_provider="groq"))


@pytest.mark.parametrize("provider", sorted(PRESETS))
def test_each_provider_preset_builds_a_deterministic_client(provider):
    model = make_chat_model(make_settings(llm_provider=provider, llm_model="some-model", llm_api_key="k"))
    assert model.temperature == 0 and PRESETS[provider] in str(model.openai_api_base)


def test_a_custom_base_url_overrides_the_preset():
    model = make_chat_model(
        make_settings(llm_provider="openai", llm_model="m", llm_api_key="k", llm_base_url="http://localhost:9999/v1")
    )
    assert "9999" in str(model.openai_api_base)
