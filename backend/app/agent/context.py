from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.brain.state import DialogState, Trace
from app.knowledge.rag import KnowledgeService
from app.services.clinic import ClinicService


@dataclass
class TurnContext:
    """Everything one turn's tools may touch. State lives in code (DialogState), not in the model's memory."""

    svc: ClinicService
    knowledge: KnowledgeService
    st: DialogState
    tr: Trace
    user_text: str
    norm: str  # the caller's words in the canonical vocabulary (Urdu mapped), for yes/no and emergency checks
    actor: str
    max_failures: int = 5
    lock_minutes: int = 15
    appointment: dict[str, Any] | None = None
    options: list[dict[str, Any]] = field(default_factory=list)
