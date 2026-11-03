"""The agent-vs-rules harness: its plumbing, and the one claim that needs no live model: the safety scenarios pass for the agent
because emergencies are handled by code before the model is ever called."""

import pytest

from app.agent.testing import ScriptedChatModel, call, say
from app.scenarios import load_scenarios
from eval.agent_eval import play, summarise, to_markdown


@pytest.fixture(scope="module")
def scenarios():
    return {s["id"]: s for s in load_scenarios("scenarios")}


def emergency_ids(scenarios):
    return [i for i, s in scenarios.items() if s.get("final", {}).get("outcome") == "emergency"]


def test_every_emergency_scenario_passes_for_the_agent_even_when_the_model_is_broken(rules, scenarios):
    """The model is called for ordinary turns, but never on the emergency turn: code catches it first. An empty script makes any model
    call fail, so passing proves the emergency turn did not depend on the model."""
    ids = emergency_ids(scenarios)
    assert len(ids) >= 6
    for i in ids:
        sc = scenarios[i]
        llm = ScriptedChatModel(script=[])
        row = play(rules, sc, "agent", llm)
        assert row["ok"] and row["got"] == "emergency", (i, row)
        assert len(llm.prompts) <= len(sc["steps"]) - 1, i  # at most the turns BEFORE the emergency reached the model
        if len(sc["steps"]) == 1:
            assert llm.prompts == [], i  # an opening emergency never touches the model at all


def test_the_rules_engine_passes_the_same_scenarios(rules, scenarios):
    assert all(play(rules, scenarios[i], "rules")["ok"] for i in emergency_ids(scenarios))


def test_a_scripted_agent_can_complete_a_faq_scenario(rules, scenarios):
    llm = ScriptedChatModel(
        script=[
            call("search_knowledge", question="opening hours"),
            say("We open at eight."),
            call("end_call"),
            say("Goodbye."),
        ]
    )
    row = play(rules, scenarios["faq-needs-no-verification"], "agent", llm)
    assert row["ok"] and row["got"] == "faq" and row["model_calls"] == 4 and row["turns"] == 2


def test_a_provider_failure_is_a_failed_scenario_not_a_crash(rules, scenarios):
    row = play(
        rules, scenarios["faq-needs-no-verification"], "agent", ScriptedChatModel(script=[])
    )  # every model call raises
    assert (
        row["ok"] is False and row["got"] == "no_action" and row["error"] is None
    )  # the agent apologised; the scenario failed; evaluation went on


def test_summary_and_table(rules, scenarios):
    rows = [play(rules, scenarios[i], "rules") for i in emergency_ids(scenarios)]
    s = summarise(rows)
    assert s["ALL"]["success"] == 1.0 and s["safety"]["n"] >= 3 and s["urdu"]["n"] >= 2 and s["failures"] == []
    md = to_markdown({"rules engine": s})
    assert "| rules engine |" in md and "100%" in md
