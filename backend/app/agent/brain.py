"""AgentBrain: the same interface as the rules `Brain` (start / handle -> TurnResult), so web, WhatsApp and the staff console
work unchanged with either. The LLM plans and phrases; deterministic code owns state, safety and every write."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.context import TurnContext
from app.agent.graph import build_graph
from app.agent.tools import build_tools
from app.brain import urdu
from app.brain.dialog import Brain, TurnResult
from app.brain.state import PROGRESS_STEPS, DialogState, Trace
from app.brain.text import normalize
from app.knowledge.rag import KnowledgeService
from app.services.clinic import ClinicService

log = logging.getLogger("agent")
HISTORY_LIMIT = 20  # messages kept across turns (user + assistant text only; tool traffic is per-turn)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    return " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content).strip()


class AgentBrain:
    def __init__(
        self,
        svc: ClinicService,
        knowledge: KnowledgeService,
        llm: Any,
        actor: str,
        rules_brain: Brain | None = None,
        max_steps: int = 6,
        max_failures: int = 5,
        lock_minutes: int = 15,
    ):
        self.svc, self.knowledge, self.llm, self.actor = svc, knowledge, llm, actor
        self.rules_brain, self.max_steps = rules_brain, max_steps
        self.max_failures, self.lock_minutes = max_failures, lock_minutes

    # ------------------------------------------------------------------ prompt
    def _system_prompt(self, ctx: TurnContext, asr: float | None) -> str:
        st, rules = ctx.st, self.svc.rules
        who = (
            f"verified patient, first name {st.patient_first}, {'established' if st.established else 'new'}"
            if st.verified
            else "NOT verified yet"
        )
        pending = st.pending.get("summary") if st.pending.get("action") else "none"
        note = (
            f"\nSpeech recognition confidence for the last message was {asr:.2f}; if it is low and the message is ambiguous, confirm what you heard first."
            if asr is not None and asr < 0.55
            else ""
        )
        return (
            f"You are the automated phone assistant for {rules.clinic.name}. You book, reschedule and cancel appointments, list upcoming ones, and answer "
            f"questions about the clinic.\n"
            "Rules you must follow:\n"
            "- Reply in the language the caller uses (English or Urdu). Keep replies short and natural for speech: no lists, no markdown.\n"
            "- Never invent clinic facts, availability or appointments. Use the tools. For hours, policies, services or providers call search_knowledge and "
            "answer only from its results; if nothing relevant comes back, say you are not sure and offer a person.\n"
            "- Before any appointment work, verify the caller with verify_patient (full name and date of birth as YYYY-MM-DD) or register them with "
            "register_patient if they say they are new. Never say which detail failed.\n"
            "- To change anything, call a propose_* tool first, read the summary back, and ask the caller to confirm. Only in a LATER turn, after they say yes, "
            "call confirm_pending. If a fee warning is returned, say so before asking.\n"
            "- If the caller is frustrated, asks for a person, or you cannot help after two tries, call handoff_to_staff. When they are finished, call end_call.\n"
            f"Facts from the system (authoritative): today is {self.svc.now():%A, %B %d, %Y}. Caller: {who}. Staged action awaiting confirmation: {pending}.{note}"
        )

    # ------------------------------------------------------------------ interface
    def start(self, state: DialogState) -> TurnResult:
        if self.rules_brain is None:
            raise RuntimeError("AgentBrain needs a rules Brain for the greeting")
        return self.rules_brain.start(state)

    def handle(self, state: DialogState, text: str, asr_confidence: float | None = None) -> TurnResult:
        st = state
        st.turn += 1
        spoken_urdu = urdu.is_urdu(text)
        if spoken_urdu:
            st.lang = "ur"
        norm = normalize(urdu.to_canonical(text, st.ur_names) if spoken_urdu else text)
        tr = Trace(st.turn, text, norm)
        if not text.strip():
            return self._silence(st, tr)
        st.silences = 0
        ctx = TurnContext(
            self.svc, self.knowledge, st, tr, text, norm, self.actor, self.max_failures, self.lock_minutes
        )
        history = [
            HumanMessage(m["text"]) if m["role"] == "user" else AIMessage(m["text"])
            for m in st.agent.get("messages", [])
        ]
        try:
            graph = build_graph(
                self.llm, build_tools(ctx), ctx, lambda: self._system_prompt(ctx, asr_confidence), self.max_steps
            )
            out = graph.invoke(
                {"messages": [*history, HumanMessage(text)], "steps": 0, "final": None},
                config={"recursion_limit": 2 * self.max_steps + 8},
            )
            reply = out.get("final") or _text(out["messages"][-1].content)
            tr.step("note", "Model calls", f"{out['steps']} this turn")
        except Exception as exc:  # noqa: BLE001 - the model is a dependency like any other: degrade, never crash the call
            log.warning(
                "agent turn failed (%s); falling back to the rules engine", exc.__class__.__name__, exc_info=True
            )
            if self.rules_brain is None:
                reply = "Sorry, I'm having trouble right now. Please try again, or ask for a person."
                tr.step("warn", "Language model unavailable", exc.__class__.__name__)
            else:
                st.turn -= 1
                result = self.rules_brain.handle(st, text, asr_confidence)
                result.trace["steps"].insert(
                    0,
                    {
                        "kind": "warn",
                        "label": "Language model unavailable",
                        "detail": f"{exc.__class__.__name__}: answered by the rules engine",
                    },
                )
                return result
        if not reply:
            reply = "Sorry, could you say that again?"
        st.agent["messages"] = (
            [*st.agent.get("messages", []), {"role": "user", "text": text}, {"role": "assistant", "text": reply}]
        )[-HISTORY_LIMIT:]
        st.last_reply = reply
        return self._finish(st, tr, ctx, reply)

    # ------------------------------------------------------------------ helpers
    def _silence(self, st: DialogState, tr: Trace) -> TurnResult:
        st.silences += 1
        if st.silences >= 2:
            st.ended, st.end_reason, st.step = True, "silence", "ENDED"
            reply = "I couldn't hear you, so I'm ending the call. Please call again any time. Goodbye."
            tr.step("rule", "Silence", "2/2: ending the call")
        else:
            reply = "Are you still there?"
            tr.step("rule", "Silence", "1/2: asking if the caller is still there")
        ctx = TurnContext(self.svc, self.knowledge, st, tr, "", "", self.actor)
        st.last_reply = reply
        return self._finish(st, tr, ctx, reply)

    def _finish(self, st: DialogState, tr: Trace, ctx: TurnContext, reply: str) -> TurnResult:
        if st.ended:
            stage = 4
        elif st.pending.get("action"):
            stage = 3
        elif ctx.options or st.slots:
            stage = 2
        elif st.verified:
            stage = 1
        else:
            stage = 0
        if any(a in st.actions for a in ("booked", "rescheduled", "cancelled")) and ctx.appointment:
            stage = 4
        slots = (
            [{"label": "Staged action", "value": st.pending["summary"]}]
            if st.pending.get("action")
            else [{"label": k.capitalize(), "value": str(v)} for k, v in st.slots.items() if v]
        )
        tr.data["reply"] = reply
        tr.data["state"] = {"from": "AGENT", "to": "ENDED" if st.ended else "AGENT", "flow": st.pending.get("action")}
        tr.data["slots"] = slots
        progress = {"steps": PROGRESS_STEPS, "current": stage, "verified": st.verified}
        return TurnResult(reply, tr.data, st, progress, st.ended, ctx.appointment)
