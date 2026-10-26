"""Every golden conversation is a regression test, and the scoreboard must stay at 100%."""

import pytest

from app.rules import load_rules
from app.scenarios import load_scenarios, run_all, run_scenario
from tests.conftest import csrf, login

SCENARIOS = load_scenarios("scenarios")
RULES = load_rules("config/clinic.yaml")


def test_there_are_plenty_of_scenarios_covering_every_flow():
    tags = {t for s in SCENARIOS for t in s.get("tags", [])}
    assert len(SCENARIOS) >= 35
    assert {
        "booking",
        "cancel",
        "reschedule",
        "list",
        "faq",
        "safety",
        "handoff",
        "corrections",
        "ambiguity",
        "speech-noise",
    } <= tags


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_golden_conversation(scenario):
    result = run_scenario(RULES, scenario)
    assert result.passed, "\n".join(result.failures)


def test_scoreboard_is_perfect_and_intent_accuracy_is_measured():
    summary = run_all(RULES, SCENARIOS)
    assert summary["completion_rate"] == 100
    assert summary["intent_accuracy"] == 100 and summary["avg_turns"] > 1


def test_a_broken_expectation_is_reported_not_swallowed():
    bad = {
        "id": "x",
        "title": "x",
        "steps": [{"say": "what time do you open", "expect": {"reply_contains": ["moon base"], "intent": "book"}}],
    }
    r = run_scenario(RULES, bad)
    assert not r.passed and any("moon base" in f for f in r.failures) and any("intent" in f for f in r.failures)
    assert r.intent_checks == 1 and r.intent_hits == 0


def test_scenarios_never_touch_the_live_database(client):
    login(client)
    before = client.get("/api/staff/stats").json()
    client.post("/api/staff/scenarios/run", headers=csrf(client))
    after = client.get("/api/staff/stats").json()
    assert (
        before["calls_total"] == after["calls_total"]
        and before["upcoming_appointments"] == after["upcoming_appointments"]
    )


def test_scenario_api(client):
    assert client.post("/api/staff/scenarios/run").status_code == 401
    login(client)
    listing = client.get("/api/staff/scenarios").json()
    assert len(listing) == len(SCENARIOS)
    assert client.post("/api/staff/scenarios/run").status_code == 403  # CSRF header required
    run = client.post("/api/staff/scenarios/run", headers=csrf(client)).json()
    assert run["completion_rate"] == 100 and run["total"] == len(SCENARIOS)
    assert run["results"][0]["transcript"][0]["reply"]


def test_duplicate_ids_are_rejected(tmp_path):
    (tmp_path / "a.yaml").write_text("- {id: dup, title: a, steps: []}\n- {id: dup, title: b, steps: []}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_scenarios(tmp_path)
