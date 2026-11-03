"""Speech-to-text endpoint for the web page. The browser records; a pretrained model transcribes; our code decides."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app import models as m
from app.api.deps import AppContext, get_ctx, get_db
from app.speech.stt import STTError, transcribe_best

router = APIRouter(prefix="/api", tags=["speech"])
log = logging.getLogger("stt")


@router.post("/stt")
async def stt(
    request: Request, call_id: str, lang: str = "en", ctx: AppContext = Depends(get_ctx), db: Session = Depends(get_db)
) -> dict:
    if ctx.stt is None:
        raise HTTPException(503, "Server-side speech recognition is not configured")
    call = db.get(m.CallSession, call_id)
    if call is None:
        raise HTTPException(404, "Call not found")
    if call.ended_at:
        raise HTTPException(409, "This call has ended")
    limit = ctx.settings.stt_max_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Audio too large")
    audio = await request.body()
    if not audio:
        raise HTTPException(422, "No audio received")
    if len(audio) > limit:
        raise HTTPException(413, "Audio too large")
    mime = request.headers.get("content-type", "audio/webm")
    hints = [lang if lang in ("en", "ur") else "en", "en" if lang == "ur" else "ur"]
    try:
        t = await run_in_threadpool(transcribe_best, ctx.stt, audio, mime, hints)
    except STTError:
        log.warning("speech recognition failed", extra={"call_id": call_id})
        ctx.metrics.inc("stt_requests_total", {"provider": ctx.stt.name, "result": "error"})
        raise HTTPException(502, "Speech recognition failed. Please type instead.") from None
    ctx.metrics.inc("stt_requests_total", {"provider": ctx.stt.name, "result": "ok"})
    return {"text": t.text, "confidence": t.confidence, "language": t.language}
