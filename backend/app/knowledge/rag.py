"""GraphRAG-style retrieval: search finds the facts, the graph says what they are connected to, and every result carries its source."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.knowledge.docs import KDoc
from app.knowledge.graph import KnowledgeGraph, ref_for
from app.knowledge.search import SearchBackend, best_per_fact


def describe_related(entry: dict[str, Any]) -> list[str]:
    """Turn graph neighbours into short, citable facts (English; the caller's language is applied by whoever renders them)."""
    rel = entry.get("related", [])

    def names(direction: str, rel_type: str, label: str) -> list[str]:
        return [r["title"] for r in rel if (r["direction"], r["rel"], r["label"]) == (direction, rel_type, label)]

    facts: list[str] = []
    label = entry["label"]
    if label == "Policy":
        if s := names("in", "GOVERNED_BY", "Service"):
            facts.append(f"Applies to: {', '.join(s)}")
        if f := names("out", "ANSWERS", "Policy") or names("in", "ANSWERS", "FAQ"):
            facts.append(f"Explained in the FAQ: {', '.join(f)}")
    elif label == "FAQ":
        if p := names("out", "ANSWERS", "Policy"):
            facts.append(f"Backed by policy: {', '.join(p)}")
        if s := names("out", "ANSWERS", "Service"):
            facts.append(f"About services: {', '.join(s)}")
    elif label == "Service":
        if p := names("in", "OFFERS", "Provider"):
            facts.append(f"Offered by: {', '.join(p)}")
        if g := names("out", "GOVERNED_BY", "Policy"):
            facts.append(f"Governed by: {', '.join(g)}")
    elif label == "Provider":
        if s := names("out", "OFFERS", "Service"):
            facts.append(f"Offers: {', '.join(s)}")
    return facts


class KnowledgeService:
    def __init__(self, docs: list[KDoc], search: SearchBackend, graph: KnowledgeGraph):
        self.docs, self.search, self.graph = docs, search, graph
        self.by_base: dict[str, dict[str, KDoc]] = defaultdict(dict)
        for d in docs:
            self.by_base[d.base_id][d.lang] = d

    def ask(self, query: str, lang: str = "en", k: int = 3) -> dict[str, Any]:
        hits = best_per_fact(self.search.search(query, max(k * 4, 12)))[:k]
        expanded = self.graph.expand([ref_for(h.doc.base_id) for h in hits])
        results: list[dict[str, Any]] = []
        suggestions: list[dict[str, Any]] = []
        for h in hits:
            doc = self.by_base[h.doc.base_id].get(lang, h.doc)
            node = expanded.get(
                f"{ref_for(h.doc.base_id)['label']}:{ref_for(h.doc.base_id)['key']}",
                {"related": [], "label": ref_for(h.doc.base_id)["label"]},
            )
            results.append(
                {
                    "id": h.doc.base_id,
                    "kind": doc.kind,
                    "title": doc.title,
                    "text": doc.text,
                    "score": round(h.score, 3),
                    "source": doc.source,
                    "facts": describe_related(node),
                }
            )
            if doc.kind == "faq":  # "suggest the right service after an FAQ"
                for s in self.graph.services_for_faq(doc.base_id.split(":", 1)[1]):
                    if not any(x["service"] == s["key"] for x in suggestions):
                        suggestions.append(
                            {
                                "service": s["key"],
                                "title": s["title"],
                                "providers": [p["title"] for p in s["providers"]],
                            }
                        )
        return {
            "query": query,
            "search_backend": self.search.name,
            "graph_backend": getattr(self.graph, "name", "?"),
            "results": results,
            "suggestions": suggestions,
            "provenance": [r["source"] for r in results],
        }

    def policies_for(self, service_key: str, lang: str = "en") -> dict[str, Any]:
        """Which rules apply to a service, with where each one comes from."""
        out = []
        for p in self.graph.policies_for_service(service_key):
            doc = self.by_base.get(f"policy:{p['key']}", {}).get(lang)
            out.append(
                {
                    "policy": p["key"],
                    "title": doc.title if doc else p["title"],
                    "text": doc.text if doc else p["text"],
                    "source": p["source"],
                }
            )
        return {"service": service_key, "policies": out}

    def status(self) -> dict[str, Any]:
        return {
            "documents": len(self.docs),
            "facts": len(self.by_base),
            "search_backend": self.search.name,
            "graph_backend": getattr(self.graph, "name", "?"),
            "graph": self.graph.stats(),
        }
