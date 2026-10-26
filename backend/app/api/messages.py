"""Inbound patient replies (SMS/WhatsApp webhook). Protected by a shared secret; disabled when none is set."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import AppContext, get_ctx, get_db, get_service
from app.services.clinic import ClinicService

router = APIRouter(prefix="/api/inbound", tags=["messages"])


class InboundIn(BaseModel):
    from_phone: str = Field(max_length=30)
    text: str = Field(max_length=500)


@router.post("/message")
def inbound(
    body: InboundIn,
    x_webhook_token: str | None = Header(default=None),
    ctx: AppContext = Depends(get_ctx),
    svc: ClinicService = Depends(get_service),
    _db: Session = Depends(get_db),
) -> dict:
    secret = ctx.settings.inbound_secret
    if not secret:
        raise HTTPException(404, "Not found")  # the endpoint does not exist until a secret is configured
    if not x_webhook_token or not hmac.compare_digest(x_webhook_token, secret):
        raise HTTPException(401, "Bad token")
    reply = svc.waitlist.handle_inbound(body.from_phone, body.text)  # type: ignore[union-attr]
    ctx.metrics.inc("messages_inbound_total", {"matched": "yes" if reply else "no"})
    return {"reply": reply}
