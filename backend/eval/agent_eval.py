"""Outcome-based comparison of the rules engine and the LLM agent on the golden scenarios.

    python -m eval.agent_eval --brain both --provider groq --model <model-name> --limit 20 --json agent_eval.json
    (set LLM_API_KEY for hosted providers; Ollama needs none)

A scenario is a success when the call ends the way the script says it should (booked, cancelled, handoff, emergency, ...), judged
from state the CODE owns, not from the model's wording. Wording cannot be compared: the rules engine says one fixed sentence and a
language model says something different every time. Also reported: turns, model calls per turn, and seconds per turn.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

from app.agent.brain import AgentBrain
from app.brain.dialog import Brain
from app.brain.retrieval import FaqIndex
from app.brain.state import DialogState
from app.config import Settings
from app.db import Database
from app.knowledge.docs import build_documents
from app.knowledge.graph import InMemoryGraph, build_graph_spec
from app.knowledge.rag import KnowledgeService
from app.knowledge.search import Bm25Search
from app.models import Base
from app.rules import ClinicRules, load_rules
from app.scenarios import PINNED_NOW, _setup, load_scenarios
from app.seed import seed_demo
from app.services.clinic import ClinicService
from app.services.engagement import LogNotifier, Outbox, WaitlistService


def play(rules: ClinicRules, sc: dict[str, Any], kind: str, llm: Any = None) -> dict[str, Any]:
    db = Database(Settings(app_env="test", database_url="sqlite:///:memory:", bcrypt_rounds=4))
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        svc = ClinicService(session, rules, now_fn=lambda: PINNED_NOW)
        svc.sync_providers()
        seed_demo(svc, PINNED_NOW)
        svc.waitlist = WaitlistService(svc, Outbox(session, LogNotifier()))
        svc.on_freed = svc.waitlist.on_slot_freed
        for action in sc.get("setup", []):
            _setup(svc, action)
        docs = build_documents(rules)
        knowledge = KnowledgeService(docs, Bm25Search(docs), InMemoryGraph(build_graph_spec(rules, docs)))
        st = DialogState(lang=sc.get("lang", "en"), lang_locked="lang" in sc)
        brain: Any = (
            Brain(svc, FaqIndex(rules.faq), "eval")
            if kind == "rules"
            else AgentBrain(svc, knowledge, llm, "eval", None)
        )
        if kind == "rules":
            brain.start(st)
        model_calls = 0
        started = time.perf_counter()
        error = None
        for step in sc["steps"]:
            if st.ended:
                break
            try:
                r = brain.handle(st, step["say"], step.get("asr"))
            except Exception as exc:  # noqa: BLE001 - a provider failure is a failed scenario, not a crashed evaluation
                error = f"{exc.__class__.__name__}: {exc}"[:120]
                break
            for s in r.trace["steps"]:
                if s["label"] == "Model calls":
                    model_calls += int(s["detail"].split()[0])
        seconds = time.perf_counter() - started
        expected = sc.get("final", {}).get("outcome")
        got = st.outcome()
        return {
            "id": sc["id"],
            "lang": sc.get("lang", "en"),
            "tags": sc.get("tags", []),
            "expected": expected,
            "got": got,
            "ok": got == expected and error is None,
            "turns": st.turn,
            "model_calls": model_calls,
            "seconds_per_turn": round(seconds / max(st.turn, 1), 3),
            "error": error,
        }


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def block(rs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n": len(rs),
            "success": round(sum(r["ok"] for r in rs) / len(rs), 3) if rs else None,
            "turns": round(statistics.mean(r["turns"] for r in rs), 2) if rs else None,
            "model_calls_per_turn": round(sum(r["model_calls"] for r in rs) / max(sum(r["turns"] for r in rs), 1), 2),
            "seconds_per_turn": round(statistics.mean(r["seconds_per_turn"] for r in rs), 3) if rs else None,
        }

    out = {
        "ALL": block(rows),
        "safety": block([r for r in rows if "safety" in r["tags"]]),
        "urdu": block([r for r in rows if r["lang"] == "ur"]),
    }
    out["failures"] = [{k: r[k] for k in ("id", "expected", "got", "error")} for r in rows if not r["ok"]]
    return out


def to_markdown(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| Brain | scenarios | success | safety scenarios | Urdu scenarios | model calls / turn | s / turn |",
        "|---|---|---|---|---|---|---|",
    ]
    fmt = lambda b: "–" if b["success"] is None else f"{b['success']:.0%} ({b['n']})"  # noqa: E731
    for name, s in results.items():
        a = s["ALL"]
        lines.append(
            f"| {name} | {a['n']} | {fmt(a)} | {fmt(s['safety'])} | {fmt(s['urdu'])} | {a['model_calls_per_turn']} | {a['seconds_per_turn']} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", choices=["rules", "agent", "both"], default="both")
    ap.add_argument("--provider", default="groq")
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=0, help="only the first N scenarios (hosted models cost money)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    rules = load_rules("config/clinic.yaml")
    scenarios = [s for s in load_scenarios("scenarios") if s.get("final", {}).get("outcome")]
    if args.limit:
        scenarios = scenarios[: args.limit]
    results: dict[str, dict[str, Any]] = {}
    if args.brain in ("rules", "both"):
        results["rules engine"] = summarise([play(rules, s, "rules") for s in scenarios])
    if args.brain in ("agent", "both"):
        from app.agent.llm import make_chat_model

        llm = make_chat_model(
            Settings(
                llm_provider=args.provider,
                llm_model=args.model,
                llm_api_key=__import__("os").environ.get("LLM_API_KEY"),
            )
        )
        if llm is None:
            raise SystemExit("choose a provider and --model for the agent")
        results[f"agent ({args.provider}:{args.model})"] = summarise([play(rules, s, "agent", llm) for s in scenarios])
    print(f"{len(scenarios)} scenarios with a defined outcome\n\n" + to_markdown(results))
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
