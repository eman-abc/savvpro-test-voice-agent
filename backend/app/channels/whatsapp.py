"""WhatsApp (Meta Cloud API) as a second channel for the same brain.

Inbound: a signed webhook delivers text and voice notes. Voice notes are transcribed by a pretrained speech model
(perception); our rules decide everything that happens. Outbound: replies, reminders and waitlist offers go through
the Graph API, respecting WhatsApp's 24-hour rule (free-form only after the patient wrote; a template otherwise).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models as m
from app.brain import urdu
from app.brain.state import DialogState
from app.config import Settings
from app.services.engagement import SkipSend, interpret_reply, patient_for_phone
from app.speech.stt import STTError, transcribe_best
from app.turns import run_turn

if TYPE_CHECKING:
    from app.api.deps import AppContext

log = logging.getLogger("whatsapp")
WINDOW = timedelta(hours=24)
WINDOW_CLOSED_CODES = {131047, 131026}  # "re-engagement message" / "message undeliverable"
ACTIVE_STEPS = {"IDENTIFY", "REGISTER", "COLLECT", "OFFER", "SELECT", "CONFIRM", "WAITLIST", "HEARD", "CLARIFY"}


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """Meta signs every webhook body: header `X-Hub-Signature-256: sha256=<HMAC-SHA256(body, app secret)>`."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256=") :])


@dataclass
class Inbound:
    kind: str  # text | audio | other
    sender: str
    message_id: str
    text: str = ""
    media_id: str | None = None
    mime: str | None = None


@dataclass
class StatusEvent:
    message_id: str
    status: str  # sent | delivered | read | failed
    recipient: str
    error: str | None = None


def parse_webhook(payload: dict[str, Any]) -> tuple[list[Inbound], list[StatusEvent]]:
    msgs: list[Inbound] = []
    stats: list[StatusEvent] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            v = change.get("value") or {}
            for mm in v.get("messages") or []:
                kind, sender, mid = mm.get("type"), digits(mm.get("from")), mm.get("id") or ""
                if kind == "text":
                    msgs.append(Inbound("text", sender, mid, text=(mm.get("text") or {}).get("body") or ""))
                elif kind == "audio":
                    a = mm.get("audio") or {}
                    msgs.append(Inbound("audio", sender, mid, media_id=a.get("id"), mime=a.get("mime_type")))
                else:
                    msgs.append(Inbound("other", sender, mid))
            for st in v.get("statuses") or []:
                errs = st.get("errors") or []
                err = (
                    f"{errs[0].get('code')}: {errs[0].get('title') or errs[0].get('message') or ''}"[:200]
                    if errs
                    else None
                )
                stats.append(
                    StatusEvent(st.get("id") or "", st.get("status") or "", digits(st.get("recipient_id")), err)
                )
    return msgs, stats


class WhatsAppClient:
    """Graph API calls. The access token is an environment variable and never appears in logs."""

    def __init__(
        self, token: str, phone_number_id: str, version: str, base: str, transport: httpx.BaseTransport | None = None
    ):
        self.token, self.phone_number_id = token, phone_number_id
        self.base = f"{base.rstrip('/')}/{version}"
        self._http = httpx.Client(timeout=20, transport=transport)

    @classmethod
    def from_settings(cls, s: Settings, transport: httpx.BaseTransport | None = None) -> WhatsAppClient:
        return cls(
            s.whatsapp_access_token or "",
            s.whatsapp_phone_number_id or "",
            s.whatsapp_api_version,
            s.whatsapp_graph_base,
            transport,
        )

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _raise_for(self, r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        raise WhatsAppError(str(err.get("message") or f"HTTP {r.status_code}")[:200], err.get("code"))

    def _post_message(self, body: dict[str, Any]) -> str | None:
        try:
            r = self._http.post(f"{self.base}/{self.phone_number_id}/messages", json=body, headers=self._auth)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"WhatsApp unreachable: {exc.__class__.__name__}") from exc
        self._raise_for(r)
        msgs = r.json().get("messages") or []
        return msgs[0].get("id") if msgs else None

    def send_text(self, to: str, text: str) -> str | None:
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text[:4000]},
        }
        return self._post_message(body)

    def send_template(self, to: str, name: str, lang: str, params: list[str]) -> str | None:
        clean = [re.sub(r"\s+", " ", p).strip()[:1000] for p in params]  # templates reject newlines and long space runs
        body = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": lang},
                "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in clean]}],
            },
        }
        return self._post_message(body)

    def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        try:
            meta = self._http.get(f"{self.base}/{media_id}", headers=self._auth)
            self._raise_for(meta)
            info = meta.json()
            if int(info.get("file_size") or 0) > max_bytes:
                raise WhatsAppError("media too large")
            blob = self._http.get(info["url"], headers=self._auth)  # the media URL also needs the token
            self._raise_for(blob)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"media download failed: {exc.__class__.__name__}") from exc
        if len(blob.content) > max_bytes:
            raise WhatsAppError("media too large")
        return blob.content, info.get("mime_type") or blob.headers.get("content-type", "audio/ogg")


class WhatsAppNotifier:
    """Sends reminders and offers on WhatsApp. Free-form text only inside the 24-hour window; otherwise a template."""

    def __init__(self, client: WhatsAppClient, db: Session, settings: Settings):
        self.client, self.db, self.settings = client, db, settings

    def send(self, phone: str, text: str) -> str | None:
        to = digits(phone)
        allowed = self.settings.whatsapp_allowed
        if allowed and to not in allowed:
            raise SkipSend("skipped", "number is not in WHATSAPP_ALLOWED_NUMBERS")
        contact = self.db.get(m.WhatsAppContact, to)
        window_open = bool(contact and m.utcnow() - contact.last_inbound_at < WINDOW)
        try:
            if window_open:
                return self.client.send_text(to, text)
            if self.settings.whatsapp_template_name:
                lang = "ur" if urdu.has_urdu_script(text) else "en"
                return self.client.send_template(to, self.settings.whatsapp_template_name, lang, [text])
        except WhatsAppError as exc:
            if exc.code in WINDOW_CLOSED_CODES:
                raise SkipSend("window_closed", str(exc)) from exc
            raise
        raise SkipSend("window_closed", "the 24-hour window is closed and no template is configured")


# ---------------------------------------------------------------------------- inbound ---
def _bilingual(en: str, ur: str) -> str:
    return f"{en}\n{ur}"


def _active_chat(db: Session, ctx: AppContext, sender: str) -> m.CallSession | None:
    call = db.scalar(
        select(m.CallSession)
        .where(m.CallSession.channel == "whatsapp", m.CallSession.contact == sender, m.CallSession.ended_at.is_(None))
        .order_by(m.CallSession.started_at.desc())
    )
    if call is None:
        return None
    if m.utcnow() - call.last_activity_at > timedelta(minutes=ctx.settings.call_idle_minutes):
        call.ended_at, call.outcome = m.utcnow(), "timeout"
        db.commit()
        return None
    return call


def _new_chat(db: Session, sender: str) -> m.CallSession:
    patient = patient_for_phone(db, sender)
    st = DialogState(lang=(patient.preferred_lang if patient and patient.preferred_lang in ("en", "ur") else "en"))
    call = m.CallSession(id=str(uuid.uuid4()), channel="whatsapp", contact=sender, state_json=st.to_json())
    db.add(call)
    db.flush()
    return call


def _transcribe(
    ctx: AppContext, client: WhatsAppClient, ev: Inbound, lang_hint: str
) -> tuple[str, float | None] | None:
    if ctx.stt is None or not ev.media_id:
        return None
    try:
        audio, mime = client.download_media(ev.media_id, ctx.settings.stt_max_bytes)
        t = transcribe_best(ctx.stt, audio, mime, [lang_hint, "en" if lang_hint == "ur" else "ur"])  # type: ignore[arg-type]
    except (WhatsAppError, STTError):
        log.warning("voice note could not be transcribed")
        return None
    return (t.text, t.confidence) if t.text else None


def handle_message(ctx: AppContext, db: Session, svc, client: WhatsAppClient, ev: Inbound) -> None:  # noqa: ANN001
    s = ctx.settings
    allowed = s.whatsapp_allowed
    if allowed and ev.sender not in allowed:
        log.info("ignored a message from a number outside WHATSAPP_ALLOWED_NUMBERS")
        return
    # A patient message opens (or refreshes) the 24-hour window. Two requests for a brand-new number can race to
    # create the row, so a unique-key clash is handled by updating the existing one.
    try:
        contact = db.get(m.WhatsAppContact, ev.sender)
        if contact is None:
            db.add(m.WhatsAppContact(phone=ev.sender))
        else:
            contact.last_inbound_at = m.utcnow()
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(m.WhatsAppContact, ev.sender)
        if existing is not None:
            existing.last_inbound_at = m.utcnow()
            db.commit()
    # Idempotency: Meta retries webhooks, and a retry can arrive while the original is still being processed. Checking
    # first and inserting later would let both through, so insert first and let the primary key decide who wins.
    if ev.message_id:
        try:
            db.add(m.ProcessedMessage(id=ev.message_id))
            db.commit()
        except IntegrityError:
            db.rollback()
            ctx.metrics.inc("whatsapp_messages_total", {"kind": ev.kind, "result": "duplicate"})
            return

    chat = _active_chat(db, ctx, ev.sender)
    lang_hint = (DialogState.from_json(chat.state_json).lang if chat else None) or (
        (patient_for_phone(db, ev.sender) or m.Patient(preferred_lang="en")).preferred_lang or "en"
    )

    text, asr, heard = ev.text, None, None
    if ev.kind == "audio":
        got = _transcribe(ctx, client, ev, lang_hint) if ctx.stt is not None else None
        if ctx.stt is None:
            client.send_text(
                ev.sender,
                _bilingual(
                    "Voice notes are not enabled yet. Please type your message.",
                    "وائس نوٹ ابھی فعال نہیں ہے۔ براہ کرم اپنا پیغام لکھیں۔",
                ),
            )
            return
        if got is None:
            client.send_text(
                ev.sender,
                _bilingual(
                    "Sorry, I couldn't understand that voice note. Please type your message.",
                    "معذرت، میں وائس نوٹ سمجھ نہیں سکا۔ براہ کرم اپنا پیغام لکھیں۔",
                ),
            )
            ctx.metrics.inc("whatsapp_messages_total", {"kind": "audio", "result": "unintelligible"})
            return
        text, asr = got
        heard = text
    elif ev.kind == "other":
        client.send_text(
            ev.sender,
            _bilingual("I can read text messages and voice notes.", "میں متن کے پیغامات اور وائس نوٹ سمجھ سکتا ہوں۔"),
        )
        return
    if not text.strip():
        return

    # A bare "1" or "yes" answers a reminder or waitlist offer, unless the caller is in the middle of a conversation.
    in_chat = bool(chat and DialogState.from_json(chat.state_json).step in ACTIVE_STEPS)
    if not in_chat and interpret_reply(text) and svc.waitlist.has_open_notification(ev.sender):
        svc.waitlist.handle_inbound(ev.sender, text)  # the reply is sent by the outbox, over the same notifier
        ctx.metrics.inc("whatsapp_messages_total", {"kind": ev.kind, "result": "notification_reply"})
        return

    call = chat or _new_chat(db, ev.sender)
    r, _st = run_turn(db, svc, ctx, call, text, asr)
    reply = f"🎤 “{heard}”\n\n{r.reply}" if heard else r.reply
    client.send_text(ev.sender, reply)
    ctx.metrics.inc("whatsapp_messages_total", {"kind": ev.kind, "result": "chat"})


def apply_status(db: Session, ev: StatusEvent) -> None:
    note = db.scalar(select(m.Notification).where(m.Notification.external_id == ev.message_id))
    if note is None:
        return
    order = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
    if order.get(ev.status, 0) >= order.get(note.delivery or "", 0):
        note.delivery = ev.status
    if ev.status == "failed":
        note.error = ev.error
        if note.status == "sent":
            note.status = "failed"
    db.commit()


def process_payload(ctx: AppContext, payload: dict[str, Any]) -> None:
    """Run in the background after the webhook has already answered 200 (Meta retries slow or failing webhooks)."""
    from app.api.deps import make_service

    client = ctx.whatsapp
    if client is None:
        return
    msgs, stats = parse_webhook(payload)
    with ctx.db.session() as db:
        svc = make_service(db, ctx.settings, ctx.rules, whatsapp=client)  # type: ignore[arg-type]
        for st in stats:
            apply_status(db, st)
        for ev in msgs:
            try:
                handle_message(ctx, db, svc, client, ev)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - one bad message must not stop the others
                log.exception("failed to handle a WhatsApp message")
                db.rollback()
                with contextlib.suppress(Exception):
                    client.send_text(
                        ev.sender,
                        _bilingual(
                            "Sorry, something went wrong. Please try again.",
                            "معذرت، کچھ غلط ہو گیا۔ براہ کرم دوبارہ کوشش کریں۔",
                        ),
                    )  # type: ignore[union-attr]
