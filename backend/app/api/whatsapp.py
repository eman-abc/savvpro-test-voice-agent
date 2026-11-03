"""WhatsApp webhook: Meta's verification handshake, and the signed delivery of messages and status updates."""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import AppContext, get_ctx
from app.channels.whatsapp import process_payload, verify_signature

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


def _enabled(ctx: AppContext) -> None:
    if not ctx.settings.whatsapp_enabled:
        raise HTTPException(404, "Not found")  # the endpoint does not exist until WhatsApp is configured


@router.get("/webhook")
def verify(
    mode: str | None = Query(default=None, alias="hub.mode"),
    token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
    ctx: AppContext = Depends(get_ctx),
) -> PlainTextResponse:
    """Meta calls this once when you save the webhook URL: echo the challenge only if the verify token matches."""
    _enabled(ctx)
    expected = ctx.settings.whatsapp_verify_token or ""
    if mode != "subscribe" or not token or not hmac.compare_digest(token, expected) or challenge is None:
        raise HTTPException(403, "Verification failed")
    return PlainTextResponse(challenge)


@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks, ctx: AppContext = Depends(get_ctx)) -> dict:
    _enabled(ctx)
    raw = await request.body()
    if not verify_signature(ctx.settings.whatsapp_app_secret or "", raw, request.headers.get("x-hub-signature-256")):
        ctx.metrics.inc("whatsapp_webhooks_total", {"result": "bad_signature"})
        raise HTTPException(401, "Bad signature")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON") from None
    ctx.metrics.inc("whatsapp_webhooks_total", {"result": "ok"})
    # Answer 200 immediately: Meta retries slow webhooks, and processing (speech-to-text, the brain) takes a moment.
    background.add_task(process_payload, ctx, payload)
    return {"ok": True}
