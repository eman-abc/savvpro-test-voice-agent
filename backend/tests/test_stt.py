"""Speech-to-text: a pretrained model produces text and a confidence number; our code decides what to do."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.speech.stt import DeepgramSTT, FakeSTT, STTError, make_stt, transcribe_best
from tests.conftest import make_settings
from tests.wa_helpers import FakeMeta


def dg(meta: FakeMeta) -> DeepgramSTT:
    return DeepgramSTT("dg-key", "nova-3", "https://api.deepgram.com", meta.transport)


def test_deepgram_request_is_built_correctly_and_the_response_parsed():
    meta = FakeMeta()
    t = dg(meta).transcribe(b"audio-bytes", "audio/webm;codecs=opus", "ur")
    assert (t.text, t.confidence, t.language) == ("what time do you open", 0.93, "ur")
    req = meta.deepgram[0]
    assert req["params"] == {"model": "nova-3", "language": "ur", "smart_format": "true", "punctuate": "true"}
    assert req["headers"]["authorization"] == "Token dg-key"
    assert req["headers"]["content-type"] == "audio/webm"  # codec parameters are stripped
    assert req["size"] == len(b"audio-bytes")


@pytest.mark.parametrize("status", [401, 429, 500])
def test_provider_errors_become_stt_errors(status):
    meta = FakeMeta()
    meta.dg_status = status
    with pytest.raises(STTError, match=str(status)):
        dg(meta).transcribe(b"x", "audio/webm", "en")


def test_malformed_and_unreachable_responses_become_stt_errors():
    bad = DeepgramSTT("k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"oops": 1})))
    with pytest.raises(STTError, match="unexpected"):
        bad.transcribe(b"x", "audio/webm", "en")

    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(STTError, match="unreachable"):
        DeepgramSTT("k", transport=httpx.MockTransport(boom)).transcribe(b"x", "audio/webm", "en")


def test_empty_transcript_is_returned_as_empty_text():
    meta = FakeMeta()
    meta.dg_queue = [{"transcript": "", "confidence": 0.0}]
    assert dg(meta).transcribe(b"x", "audio/webm", "en").text == ""


def test_a_confident_first_language_needs_only_one_call():
    meta = FakeMeta()
    t = transcribe_best(dg(meta), b"x", "audio/webm", ["ur", "en"])
    assert t.language == "ur" and len(meta.deepgram) == 1


def test_a_weak_first_result_tries_the_other_language_and_keeps_the_better_one():
    meta = FakeMeta()
    meta.dg_queue = [{"transcript": "garbled", "confidence": 0.2}, {"transcript": "book a flu shot", "confidence": 0.9}]
    t = transcribe_best(dg(meta), b"x", "audio/webm", ["ur", "en"])
    assert (t.text, t.language) == ("book a flu shot", "en")
    assert [r["params"]["language"] for r in meta.deepgram] == ["ur", "en"]


def test_the_less_weak_result_wins_when_both_are_weak():
    meta = FakeMeta()
    meta.dg_queue = [{"transcript": "a", "confidence": 0.1}, {"transcript": "b", "confidence": 0.4}]
    assert transcribe_best(dg(meta), b"x", "audio/webm", ["en", "ur"]).text == "b"


def test_if_every_attempt_fails_the_error_surfaces():
    meta = FakeMeta()
    meta.dg_status = 500
    with pytest.raises(STTError):
        transcribe_best(dg(meta), b"x", "audio/webm", ["en", "ur"])


def test_provider_selection_and_validation():
    assert make_stt(Settings(app_env="test", stt_provider="none")) is None
    assert isinstance(make_stt(Settings(app_env="test", stt_provider="fake")), FakeSTT)
    with pytest.raises(ValueError, match="DEEPGRAM_API_KEY"):
        Settings(app_env="test", stt_provider="deepgram")
    with pytest.raises(ValueError, match="must be none"):
        Settings(app_env="test", stt_provider="whisper")
    with pytest.raises(ValueError, match="not allowed in prod"):
        Settings(app_env="prod", jwt_secret="p" * 32, stt_provider="fake")


# ----------------------------------------------------------------------- the endpoint ---
@pytest.fixture
def stt_client():
    meta = FakeMeta()
    app = create_app(make_settings(stt_provider="deepgram", deepgram_api_key="dg-key"), http_transport=meta.transport)
    with TestClient(app) as c:
        yield c, meta


def new_call(c) -> str:
    return c.post("/api/calls").json()["call_id"]


def test_endpoint_transcribes_and_reports_confidence(stt_client):
    c, meta = stt_client
    cid = new_call(c)
    r = c.post(
        f"/api/stt?call_id={cid}&lang=ur",
        content=b"\x1aE\xdf\xa3 webm",
        headers={"Content-Type": "audio/webm;codecs=opus"},
    )
    assert r.status_code == 200 and r.json() == {"text": "what time do you open", "confidence": 0.93, "language": "ur"}
    assert meta.deepgram[0]["params"]["language"] == "ur"


def test_endpoint_guards(stt_client):
    c, _ = stt_client
    cid = new_call(c)
    assert c.post("/api/stt?call_id=nope", content=b"x").status_code == 404
    assert c.post(f"/api/stt?call_id={cid}", content=b"").status_code == 422
    big = b"x" * (c.app.state.ctx.settings.stt_max_bytes + 1)
    assert c.post(f"/api/stt?call_id={cid}", content=big).status_code == 413
    c.post(f"/api/calls/{cid}/turn", json={"text": "goodbye"})  # ends the call
    assert c.post(f"/api/stt?call_id={cid}", content=b"x").status_code == 409


def test_endpoint_hides_provider_failures(stt_client):
    c, meta = stt_client
    cid = new_call(c)
    meta.dg_status = 500
    r = c.post(f"/api/stt?call_id={cid}", content=b"x")
    assert r.status_code == 502 and "dg-key" not in r.text and "500" not in r.text


def test_endpoint_is_off_without_a_provider(client):
    cid = client.post("/api/calls").json()["call_id"]
    assert client.post(f"/api/stt?call_id={cid}", content=b"x").status_code == 503
    assert client.get("/api/config").json()["stt"] is False


def test_config_reports_server_side_speech(stt_client):
    assert stt_client[0].get("/api/config").json()["stt"] is True


def test_speech_metrics_are_counted(stt_client):
    c, _ = stt_client
    cid = new_call(c)
    c.post(f"/api/stt?call_id={cid}", content=b"x")
    assert 'stt_requests_total{provider="deepgram",result="ok"} 1' in c.get("/metrics").text
