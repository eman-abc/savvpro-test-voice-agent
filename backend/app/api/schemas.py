from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TurnIn(BaseModel):
    text: str = Field(default="", max_length=500)
    seq: int | None = Field(default=None, ge=0, description="turns the client has seen; makes retries idempotent")


class StartIn(BaseModel):
    lang: Literal["en", "ur"] = "en"


class TurnOut(BaseModel):
    call_id: str
    reply: str
    trace: dict[str, Any]
    progress: dict[str, Any]
    ended: bool
    lang: str = "en"
    outcome: str | None = None
    appointment: dict[str, Any] | None = None


class LoginIn(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)


class CancelIn(BaseModel):
    reason: str = Field(default="staff cancelled", max_length=80)


class ReplyIn(BaseModel):
    text: str = Field(max_length=300)


class RunRemindersIn(BaseModel):
    hours: int = Field(default=24, ge=1, le=24 * 30)
