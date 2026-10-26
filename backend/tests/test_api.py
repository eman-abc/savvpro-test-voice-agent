from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import csrf, login, make_settings


# ------------------------------------------------------------------- public API ---
def test_health_and_ready(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_config_exposes_demo_hints_only_in_demo_mode(client):
    cfg = client.get("/api/config").json()
    assert cfg["clinic"]["name"] and len(cfg["demo_patients"]) == 12 and cfg["demo_staff"]["username"] == "reception"
    with TestClient(create_app(make_settings(demo_mode=False))) as prod_like:
        cfg = prod_like.get("/api/config").json()
        assert "demo_patients" not in cfg and "demo_staff" not in cfg


def test_full_call_over_http(client):
    start = client.post("/api/calls")
    assert start.status_code == 201
    body = start.json()
    cid = body["call_id"]
    assert "Maple Street" in body["reply"] and body["progress"]["current"] == 0

    def turn(text):
        r = client.post(f"/api/calls/{cid}/turn", json={"text": text})
        assert r.status_code == 200, r.text
        return r.json()

    turn("I need a flu shot")
    t = turn("Priya Sharma, July 23rd 1990")
    assert t["progress"]["verified"] and t["trace"]["state"]["to"] == "COLLECT"
    turn("any provider")
    t = turn("tomorrow at 10 am")
    assert t["trace"]["state"]["to"] == "CONFIRM"
    t = turn("yes")
    assert t["appointment"]["action"] == "booked" and t["trace"]["action"]["name"] == "book_appointment"
    t = turn("no thanks")
    assert t["ended"] and t["outcome"] == "booked"
    assert client.post(f"/api/calls/{cid}/turn", json={"text": "hello"}).status_code == 409
    log = client.get(f"/api/calls/{cid}").json()
    assert log["ended"] and len(log["turns"]) == 7


def test_unknown_call_and_bad_input(client):
    assert client.post("/api/calls/does-not-exist/turn", json={"text": "hi"}).status_code == 404
    cid = client.post("/api/calls").json()["call_id"]
    assert client.post(f"/api/calls/{cid}/turn", json={"text": "x" * 501}).status_code == 422
    assert client.post(f"/api/calls/{cid}/turn", json={}).status_code == 200  # silence is handled, not an error


def test_calls_are_isolated_from_each_other(client):
    a, b = (client.post("/api/calls").json()["call_id"] for _ in range(2))
    client.post(f"/api/calls/{a}/turn", json={"text": "book a physical"})
    ra = client.post(f"/api/calls/{a}/turn", json={"text": "Wei Zhang, May 17th 1995"}).json()
    rb = client.post(f"/api/calls/{b}/turn", json={"text": "what time do you open"}).json()
    assert ra["progress"]["verified"] is True and rb["progress"]["verified"] is False


def test_security_headers(client):
    r = client.get("/api/config")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and r.headers["x-request-id"]


def test_rate_limit_applies_to_api_calls():
    with TestClient(create_app(make_settings(rate_limit_per_minute=5))) as c:
        codes = [c.get("/api/config").status_code for _ in range(8)]
        assert 429 in codes and codes[0] == 200


# --------------------------------------------------------------------- staff auth ---
def test_staff_endpoints_need_a_session(client):
    for path in ("/api/staff/appointments", "/api/staff/calls", "/api/staff/stats", "/api/auth/me"):
        assert client.get(path).status_code == 401


def test_login_logout_cycle(client):
    assert login(client, password="wrong").status_code == 401
    r = login(client)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").json()["username"] == "reception"
    assert client.post("/api/auth/logout", headers=csrf(client)).status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_login_lockout_after_repeated_failures(client):
    for _ in range(5):
        assert login(client, password="nope").status_code == 401
    assert login(client, password="nope").status_code == 429
    assert login(client).status_code == 429  # even the right password is refused while locked


def test_unknown_user_gets_same_error_as_wrong_password(client):
    a, b = login(client, username="nobody", password="x"), login(client, password="x")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_tampered_token_is_rejected(client):
    login(client)
    client.cookies.set("clinic_session", "not.a.jwt")
    assert client.get("/api/auth/me").status_code == 401


def test_state_changing_calls_require_csrf_header(client):
    login(client)
    appt_id = client.get("/api/staff/appointments", params={"start": "2026-10-26", "end": "2026-11-09"}).json()[0]["id"]
    assert client.post(f"/api/staff/appointments/{appt_id}/cancel", json={}).status_code == 403
    bad = client.post(f"/api/staff/appointments/{appt_id}/cancel", json={}, headers={"X-CSRF-Token": "forged"})
    assert bad.status_code == 403
    ok = client.post(f"/api/staff/appointments/{appt_id}/cancel", json={"reason": "test"}, headers=csrf(client))
    assert ok.status_code == 200 and ok.json()["status"] == "cancelled"


# ------------------------------------------------------------------ staff features ---
def test_calendar_listing_and_filters(client):
    login(client)
    week = client.get("/api/staff/appointments", params={"start": "2026-10-26", "end": "2026-11-02"}).json()
    assert week and all(a["status"] == "booked" for a in week)
    assert {"patient", "provider", "color", "service_name", "start", "end"} <= set(week[0])
    kim = client.get(
        "/api/staff/appointments", params={"start": "2026-10-26", "end": "2026-11-09", "provider": "kim"}
    ).json()
    assert kim and all(a["provider"] == "kim" for a in kim)
    assert client.get("/api/staff/appointments", params={"start": "2026-01-01", "end": "2026-12-31"}).status_code == 422
    assert len(client.get("/api/staff/providers").json()) == 3


def test_staff_cancel_frees_the_slot_for_callers(client):
    login(client)
    appts = client.get("/api/staff/appointments", params={"start": "2026-10-28", "end": "2026-10-29"}).json()
    vacc = next(a for a in appts if a["service"] == "vaccination")  # Priya, NP Kim, Wed 10:00
    client.post(f"/api/staff/appointments/{vacc['id']}/cancel", json={}, headers=csrf(client))
    still = client.get("/api/staff/appointments", params={"start": "2026-10-28", "end": "2026-10-29"}).json()
    assert all(a["id"] != vacc["id"] for a in still)
    with_cancelled = client.get(
        "/api/staff/appointments", params={"start": "2026-10-28", "end": "2026-10-29", "include_cancelled": True}
    ).json()
    assert any(a["id"] == vacc["id"] and a["status"] == "cancelled" for a in with_cancelled)
    assert client.post("/api/staff/appointments/99999/cancel", json={}, headers=csrf(client)).status_code == 404


def test_call_log_shows_transcript_and_decision_trace(client):
    cid = client.post("/api/calls").json()["call_id"]
    client.post(f"/api/calls/{cid}/turn", json={"text": "let me talk to a real person"})
    login(client)
    calls = client.get("/api/staff/calls").json()
    assert calls[0]["id"] == cid and calls[0]["outcome"] == "handoff" and "reason" in calls[0]["handoff_summary"]
    detail = client.get(f"/api/staff/calls/{cid}").json()
    assert detail["turns"][1]["user"] == "let me talk to a real person"
    assert detail["turns"][1]["trace"]["intent"]["name"] == "handoff"
    assert client.get("/api/staff/calls/nope").status_code == 404


def test_stats(client):
    cid = client.post("/api/calls").json()["call_id"]
    client.post(f"/api/calls/{cid}/turn", json={"text": "what time do you open"})
    client.post(f"/api/calls/{cid}/turn", json={"text": "no"})
    login(client)
    s = client.get("/api/staff/stats").json()
    assert s["calls_total"] == 1 and s["outcomes"]["faq"] == 1 and s["resolved_without_human"] == 100
    assert s["upcoming_appointments"] > 0


def test_demo_reset_is_admin_and_demo_only(client):
    login(client)
    assert client.post("/api/staff/demo/reset", headers=csrf(client)).status_code == 200
    with TestClient(create_app(make_settings(demo_mode=False))) as c:
        login(c)
        assert c.post("/api/staff/demo/reset", headers=csrf(c)).status_code == 404


# ------------------------------------------------------------------- static UI ---
def test_built_ui_is_served_with_spa_fallback(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<html>app</html>")
    with TestClient(create_app(make_settings(static_dir=tmp_path))) as c:
        assert "app" in c.get("/").text
        assert "app" in c.get("/staff").text  # client-side route falls back to index.html
        assert c.get("/assets/app.js").status_code == 200
        assert c.get("/api/nope").status_code == 404 and c.get("/api/nope").json() == {"detail": "Not found"}
        assert "app" in c.get("/../../etc/passwd").text  # path traversal never escapes the static dir
        assert c.get("/api/config").status_code == 200
