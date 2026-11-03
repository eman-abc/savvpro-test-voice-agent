"""Speech-to-text behind a small interface.

The provider returns text and a confidence number. That is *perception*: our rules then decide what to do with it
(for example, read back what was heard when confidence is low). No model ever decides what the assistant does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings

log = logging.getLogger("stt")


@dataclass
class Transcript:
    text: str
    confidence: float | None
    language: str


class STTError(Exception):
    """The provider failed or rejected the audio."""


class SpeechToText(Protocol):
    name: str

    def transcribe(self, audio: bytes, mime: str, lang: str) -> Transcript: ...


class DeepgramSTT:
    """Deepgram's pre-recorded endpoint (pretrained Nova-3; Urdu is `ur`). The key lives in an env var."""

    name = "deepgram"

    def __init__(
        self,
        api_key: str,
        model: str = "nova-3",
        base_url: str = "https://api.deepgram.com",
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key, self.model, self.base_url = api_key, model, base_url.rstrip("/")
        self._client = httpx.Client(timeout=20, transport=transport)

    def transcribe(self, audio: bytes, mime: str, lang: str) -> Transcript:
        params = {"model": self.model, "language": lang, "smart_format": "true", "punctuate": "true"}
        headers = {"Authorization": f"Token {self.api_key}", "Content-Type": mime.split(";")[0] or "audio/webm"}
        try:
            r = self._client.post(f"{self.base_url}/v1/listen", params=params, headers=headers, content=audio)
        except httpx.HTTPError as exc:
            raise STTError(f"speech service unreachable: {exc.__class__.__name__}") from exc
        if r.status_code != 200:
            raise STTError(f"speech service returned {r.status_code}")
        try:
            alt = r.json()["results"]["channels"][0]["alternatives"][0]
        except (KeyError, IndexError, ValueError) as exc:
            raise STTError("unexpected response from the speech service") from exc
        text = (alt.get("transcript") or "").strip()
        return Transcript(
            text=text, confidence=float(alt["confidence"]) if "confidence" in alt else None, language=lang
        )


class FakeSTT:
    """Canned transcript for tests and demos without a key (never allowed in prod)."""

    name = "fake"

    def __init__(self, text: str):
        self.text = text

    def transcribe(self, audio: bytes, mime: str, lang: str) -> Transcript:
        return Transcript(text=self.text, confidence=0.9, language=lang)


def make_stt(settings: Settings, transport: httpx.BaseTransport | None = None) -> SpeechToText | None:
    if settings.stt_provider == "deepgram":
        return DeepgramSTT(settings.deepgram_api_key or "", settings.deepgram_model, settings.deepgram_base, transport)
    if settings.stt_provider == "fake":
        return FakeSTT(settings.fake_stt_text)
    return None


CONFIDENT = 0.6


def transcribe_best(stt: SpeechToText, audio: bytes, mime: str, hints: list[str]) -> Transcript:
    """Try the likeliest language first; only if the result is weak, try the other and keep the more confident one.

    The choice is made by comparing confidence numbers, in plain code: the model supplies numbers, we make the call.
    """
    best: Transcript | None = None
    last_error: STTError | None = None
    for lang in dict.fromkeys(hints):  # keep order, drop duplicates
        try:
            t = stt.transcribe(audio, mime, lang)
        except STTError as exc:
            last_error = exc
            continue
        if best is None or (t.confidence or 0) > (best.confidence or 0):
            best = t
        if best.text and (best.confidence or 0) >= CONFIDENT:
            break
    if best is None:
        raise last_error or STTError("no transcript")
    return best
