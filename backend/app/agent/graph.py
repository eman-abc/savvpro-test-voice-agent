"""The LangGraph state graph for one turn.

    START -> guard --(emergency)--------------------------> END
               |
               v
             agent  --(tool calls)--> tools -> agent ...   (loops; bounded by max_steps)
               |           |
               |           +--(too many steps)--> limit -> END
               +--(plain answer)--> END

`guard` and `limit` are ordinary code, not model calls: the emergency rule and the step cap cannot be talked around.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.agent.context import TurnContext
from app.brain.safety import emergency_match
from app.brain.urdu import fold_urdu


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int
    final: str | None


def build_graph(llm: Any, tools: list[Any], ctx: TurnContext, system_prompt: Callable[[], str], max_steps: int) -> Any:
    rules, st, tr = ctx.svc.rules, ctx.st, ctx.tr
    bound = llm.bind_tools(tools)

    def guard(state: AgentState) -> dict[str, Any]:
        kw = emergency_match(ctx.norm, rules.emergency_keywords) or emergency_match(
            fold_urdu(ctx.user_text).lower(), rules.emergency_keywords_ur
        )
        if kw:
            tr.step("rule", "Emergency keyword", f"matched '{kw}': the safety rule runs before the model sees anything")
            st.ended, st.end_reason, st.step = True, "emergency", "ENDED"
            number = rules.clinic.emergency_ur if st.lang == "ur" else rules.clinic.emergency_en
            text = (
                f"اگر یہ طبی ایمرجنسی ہے تو براہ کرم فون بند کریں اور فوراً {number} پر کال کریں۔ خدا حافظ۔"
                if st.lang == "ur"
                else f"If this is a medical emergency, please hang up and call {number} now. I can't help with emergencies. Please stay safe. Goodbye."
            )
            return {"final": text}
        return {}

    def agent(state: AgentState) -> dict[str, Any]:
        msg = bound.invoke([SystemMessage(system_prompt()), *state["messages"]])
        return {"messages": [msg], "steps": state["steps"] + 1}

    def limit(state: AgentState) -> dict[str, Any]:
        tr.step("warn", "Step limit reached", f"{max_steps} model calls in one turn: handing over to staff")
        st.ended, st.end_reason, st.step, st.handoff_summary = (
            True,
            "handoff",
            "ENDED",
            "the assistant could not finish the request",
        )
        return {
            "final": (
                "معذرت، مجھے اس میں دشواری ہو رہی ہے۔ میں آپ کو عملے کے کسی رکن سے ملاتا ہوں۔"
                if st.lang == "ur"
                else "I'm sorry, I'm having trouble with that. Let me pass you to a member of our team."
            )
        }

    def after_guard(state: AgentState) -> str:
        return "end" if state.get("final") else "agent"

    def after_agent(state: AgentState) -> str:
        if not getattr(state["messages"][-1], "tool_calls", None):
            return "end"
        return "limit" if state["steps"] >= max_steps else "tools"

    g = StateGraph(AgentState)
    g.add_node("guard", guard)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(tools))
    g.add_node("limit", limit)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, {"end": END, "agent": "agent"})
    g.add_conditional_edges("agent", after_agent, {"end": END, "tools": "tools", "limit": "limit"})
    g.add_edge("tools", "agent")
    g.add_edge("limit", END)
    return g.compile()
