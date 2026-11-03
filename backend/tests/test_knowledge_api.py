"""The staff-only knowledge endpoints: search with provenance, status, call insights and reindex."""

import pytest

from tests.conftest import csrf, login


def hand_off_a_call(client, text="I want to talk to a person"):
    cid = client.post("/api/calls").json()["call_id"]
    out = client.post(f"/api/calls/{cid}/turn", json={"text": text, "seq": 1}).json()
    assert out["ended"]


@pytest.mark.parametrize(
    "path", ["/api/staff/knowledge/status", "/api/staff/knowledge/search?q=hours", "/api/staff/knowledge/insights"]
)
def test_every_knowledge_endpoint_needs_a_staff_login(client, path):
    assert client.get(path).status_code == 401


def test_reindex_needs_login_and_csrf(client):
    assert client.post("/api/staff/knowledge/reindex").status_code == 401
    login(client)
    assert client.post("/api/staff/knowledge/reindex").status_code == 403  # no CSRF header


def test_status_describes_what_is_running(client):
    login(client)
    st = client.get("/api/staff/knowledge/status").json()
    assert (st["documents"], st["facts"]) == (54, 27)
    assert (st["search_backend"], st["graph_backend"]) == ("bm25-own", "in-memory")
    assert st["graph"]["relationships"]["GOVERNED_BY"] == 30


def test_search_returns_sourced_results_and_a_suggestion(client):
    login(client)
    res = client.get("/api/staff/knowledge/search", params={"q": "are you accepting new patients"}).json()
    assert res["results"] and all(r["source"].startswith("clinic.yaml") for r in res["results"])
    assert any(s["service"] == "new_patient_visit" for s in res["suggestions"])


def test_search_respects_language_and_validates_input(client):
    login(client)
    ur = client.get("/api/staff/knowledge/search", params={"q": "opening hours", "lang": "ur"}).json()["results"][0]
    assert any("؀" <= c <= "ۿ" for c in ur["text"])
    assert (
        client.get("/api/staff/knowledge/search", params={"q": "hours", "lang": "klingon"}).status_code == 200
    )  # unknown language: English
    assert client.get("/api/staff/knowledge/search").status_code == 422
    assert client.get("/api/staff/knowledge/search", params={"q": "x" * 201}).status_code == 422
    assert client.get("/api/staff/knowledge/search", params={"q": "hours", "k": 99}).status_code == 422


def test_insights_summarise_real_calls_that_ended_in_handoff(client):
    hand_off_a_call(client)
    hand_off_a_call(client)
    login(client)
    ins = client.get("/api/staff/knowledge/insights", params={"outcome": "handoff"}).json()
    assert ins["backend"] == "python" and ins["calls"] == 2
    assert ins["top_last_utterances"] == [{"text": "I want to talk to a person", "count": 2}]


def test_insights_reject_a_malformed_outcome(client):
    login(client)
    assert client.get("/api/staff/knowledge/insights", params={"outcome": "x; DROP"}).status_code == 422


def test_reindex_loads_everything_once_then_changes_nothing(client):
    login(client)
    first = client.post("/api/staff/knowledge/reindex", headers=csrf(client)).json()
    assert first["sql"] == {"added": 54, "updated": 0, "unchanged": 0, "removed": 0}
    second = client.post("/api/staff/knowledge/reindex", headers=csrf(client)).json()
    assert second["sql"] == {"added": 0, "updated": 0, "unchanged": 54, "removed": 0}


def test_reindex_refuses_invalid_knowledge_and_writes_nothing(client, monkeypatch):
    import app.knowledge.ingest as ingest

    def broken(_rules):
        raise ValueError("a policy has no text")

    monkeypatch.setattr(ingest, "build_documents", broken)
    login(client)
    r = client.post("/api/staff/knowledge/reindex", headers=csrf(client))
    assert r.status_code == 422 and "a policy has no text" in str(r.json())
