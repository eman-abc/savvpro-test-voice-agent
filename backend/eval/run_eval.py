"""Retrieval evaluation: BM25 vs Elasticsearch (four Urdu analyzers) vs dense embeddings vs hybrid fusion.

    python -m eval.run_eval --es http://localhost:9200 --out ../docs/RETRIEVAL_EVAL_RESULTS.md

Metrics are computed per fact (the English and Urdu versions of a fact count as one): Recall@k is the share of the labelled
relevant facts found in the top k, and MRR@10 is the reciprocal rank of the first relevant fact.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

import yaml

from app.knowledge.docs import build_documents
from app.knowledge.search import (
    ES_ANALYZERS,
    Bm25Search,
    DenseSearch,
    ElasticSearchBackend,
    HybridSearch,
    SearchBackend,
    best_per_fact,
)
from app.rules import load_rules

TIERS = ("direct", "paraphrase", "urdu", "urdu_variant", "roman")
# Simulates Urdu typed on an Arabic keyboard: the same words with Arabic letter forms (ي ك ه for ی ک ہ).
_ARABIC_KEYBOARD = str.maketrans({"ی": "ي", "ک": "ك", "ہ": "ه"})


def bootstrap_ci(values: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    """95% percentile bootstrap interval for the mean: how much of a difference between methods is just query luck."""
    import random

    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def with_variant_queries(queries: list[dict]) -> list[dict]:
    return queries + [
        {**q, "q": q["q"].translate(_ARABIC_KEYBOARD), "tier": "urdu_variant"} for q in queries if q["tier"] == "urdu"
    ]


def _recall(rel: set[str], ranked: list[str], k: int) -> float:
    return len(rel & set(ranked[:k])) / len(rel)


def evaluate(backend: SearchBackend, queries: list[dict]) -> dict:
    per_tier: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    times: list[float] = []
    for q in queries:
        t = time.perf_counter()
        ranked = [h.doc.base_id for h in best_per_fact(backend.search(q["q"], 20))][:10]
        times.append((time.perf_counter() - t) * 1000)
        rel = set(q["rel"])
        rr = next((1 / (i + 1) for i, b in enumerate(ranked) if b in rel), 0.0)
        row = (_recall(rel, ranked, 1), _recall(rel, ranked, 3), _recall(rel, ranked, 5), rr)
        per_tier[q["tier"]].append(row)
        if (
            q["tier"] != "urdu_variant"
        ):  # the perturbed copies are a robustness probe, not extra queries for the overall score
            per_tier["ALL"].append(row)
    out: dict = {"latency_ms": round(statistics.mean(times), 2)}
    for tier, rows in per_tier.items():
        out[tier] = {
            "n": len(rows),
            "R@1": round(statistics.mean(r[0] for r in rows), 3),
            "R@3": round(statistics.mean(r[1] for r in rows), 3),
            "R@5": round(statistics.mean(r[2] for r in rows), 3),
            "MRR": round(statistics.mean(r[3] for r in rows), 3),
        }
    lo, hi = bootstrap_ci([r[1] for r in per_tier["ALL"]])
    out["ALL"]["R@3_ci"] = [round(lo, 2), round(hi, 2)]
    lo, hi = bootstrap_ci([r[3] for r in per_tier["ALL"]])
    out["ALL"]["MRR_ci"] = [round(lo, 2), round(hi, 2)]
    return out


def build_methods(docs, es_url: str | None, with_dense: bool) -> list[SearchBackend]:
    methods: list[SearchBackend] = [Bm25Search(docs)]
    es_backends: dict[str, ElasticSearchBackend] = {}
    if es_url:
        for a in ES_ANALYZERS:
            es = ElasticSearchBackend(es_url, a, prefix="eval")
            es.ensure_index(recreate=True)
            es.index_docs(docs)
            es_backends[a] = es
            methods.append(es)
    if with_dense:
        dense = DenseSearch(docs)
        methods.append(dense)
        methods.append(HybridSearch(Bm25Search(docs), dense))
        if "urdu_custom" in es_backends:
            methods.append(HybridSearch(es_backends["urdu_custom"], dense))
    return methods


def to_markdown(results: dict[str, dict]) -> str:
    head = (
        "| Method | "
        + " | ".join(f"{t} R@3" for t in TIERS)
        + " | R@1 | **R@3** (95% CI) | **MRR** (95% CI) | ms/query |"
    )
    lines = [head, "|---|" + "---|" * (len(TIERS) + 4)]
    for name, r in results.items():
        a = r["ALL"]
        cells = [f"{r[t]['R@3']:.2f}" if t in r else "–" for t in TIERS]
        lines.append(
            f"| `{name}` | "
            + " | ".join(cells)
            + f" | {a['R@1']:.2f} | {a['R@3']:.2f} ({a['R@3_ci'][0]:.2f}–{a['R@3_ci'][1]:.2f}) | "
            f"{a['MRR']:.2f} ({a['MRR_ci'][0]:.2f}–{a['MRR_ci'][1]:.2f}) | {r['latency_ms']} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--es", default=None, help="Elasticsearch URL (omit to skip the Elasticsearch methods)")
    ap.add_argument("--no-dense", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    rules = load_rules("config/clinic.yaml")
    docs = build_documents(rules)
    queries = with_variant_queries(yaml.safe_load(Path("eval/queries.yaml").read_text(encoding="utf-8")))
    results = {m.name: evaluate(m, queries) for m in build_methods(docs, args.es, not args.no_dense)}
    md = (
        f"{len([q for q in queries if q['tier'] != 'urdu_variant'])} queries (+{len([q for q in queries if q['tier'] == 'urdu_variant'])} keyboard-variant copies of the Urdu ones) over {len({d.base_id for d in docs})} facts ({len(docs)} documents).\n\n"
        + to_markdown(results)
    )
    print(md)
    if args.out:
        Path(args.out).write_text(md + "\n", encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
