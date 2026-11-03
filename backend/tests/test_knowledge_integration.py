"""Against REAL Elasticsearch and Neo4j. Skipped unless the services are configured:

    TEST_ELASTICSEARCH_URL=http://localhost:9200  TEST_NEO4J_URI=bolt://localhost:7687  TEST_NEO4J_PASSWORD=...  pytest -m integration

(docker compose up -d elasticsearch neo4j  starts both.) Each test uses its own index prefix; the Neo4j tests only touch the labels this
project owns. Do not point them at a database you care about.
"""

import os
import uuid

import pytest
import yaml

from app.knowledge.docs import build_documents
from app.knowledge.graph import (
    STATIC_LABELS,
    InMemoryGraph,
    Neo4jGraph,
    ResilientGraph,
    build_graph_spec,
    make_graph,
    ref_for,
)
from app.knowledge.ingest import appointment_rows, ingest
from app.knowledge.search import Bm25Search, ElasticSearchBackend, ResilientSearch, make_search
from app.knowledge.transcripts import CallIndex, insights_in_python, synthetic_calls
from eval.run_eval import evaluate, with_variant_queries
from tests.conftest import make_settings

ES_URL = os.environ.get("TEST_ELASTICSEARCH_URL")
NEO_URI = os.environ.get("TEST_NEO4J_URI")
NEO_PASSWORD = os.environ.get("TEST_NEO4J_PASSWORD", "")
pytestmark = pytest.mark.integration
needs_es = pytest.mark.skipif(not ES_URL, reason="set TEST_ELASTICSEARCH_URL")
needs_neo = pytest.mark.skipif(not NEO_URI, reason="set TEST_NEO4J_URI")


@pytest.fixture
def docs(rules):
    return build_documents(rules)


@pytest.fixture
def prefix():
    p = "it" + uuid.uuid4().hex[:8]
    yield p
    if ES_URL:
        from elasticsearch import Elasticsearch

        es = Elasticsearch(ES_URL)
        found = es.options(ignore_status=[404]).indices.get(index=f"{p}_*").body
        for name in found if isinstance(found, dict) else []:  # ES refuses wildcard deletes: delete by name
            es.indices.delete(index=name)


@pytest.fixture
def es_client():
    from elasticsearch import Elasticsearch

    return Elasticsearch(ES_URL)


def wipe(graph):
    labels = (*STATIC_LABELS, "Patient", "Appointment")
    graph._read("MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) DETACH DELETE n", labels=list(labels))


@pytest.fixture
def graph():
    g = Neo4jGraph(NEO_URI, "neo4j", NEO_PASSWORD)
    wipe(g)
    yield g
    wipe(g)
    g.close()


def eval_queries():
    import pathlib

    return with_variant_queries(yaml.safe_load(pathlib.Path("eval/queries.yaml").read_text(encoding="utf-8")))


# ------------------------------------------------------------------------------------------------ Elasticsearch
@needs_es
def test_elasticsearch_finds_the_obvious_documents(docs, prefix):
    es = ElasticSearchBackend(ES_URL, "urdu_custom", prefix)
    es.ensure_index(recreate=True)
    assert es.index_docs(docs) == 54 and es.healthy()
    assert es.search("flu shot", 1)[0].doc.base_id == "service:vaccination"
    assert {h.doc.lang for h in es.search("cancellation", 10, lang="ur")} <= {"ur"}


@needs_es
def test_indexing_twice_leaves_one_copy_of_each_document(docs, prefix, es_client):
    es = ElasticSearchBackend(ES_URL, "urdu_custom", prefix)
    es.index_docs(docs)
    es.index_docs(docs)
    assert es_client.count(index=es.index)["count"] == 54


@needs_es
def test_documents_can_be_deleted_by_id(docs, prefix, es_client):
    es = ElasticSearchBackend(ES_URL, "urdu_custom", prefix)
    es.index_docs(docs)
    assert es.delete_ids(["faq:phone@en", "faq:phone@ur"]) == 2 and es.delete_ids([]) == 0
    assert es_client.count(index=es.index)["count"] == 52 and "faq:phone@en" not in es.indexed_ids()


@needs_es
def test_the_keyboard_variant_finding_holds(docs, prefix):
    """The evaluation's one clear result, as a regression test: Urdu typed with Arabic-keyboard letters breaks the standard analyzer
    but not the custom one (or the built-in Persian one)."""
    queries = [q for q in eval_queries() if q["tier"] in ("urdu", "urdu_variant")]
    out = {}
    for analyzer in ("standard", "persian", "urdu_custom"):
        es = ElasticSearchBackend(ES_URL, analyzer, prefix)
        es.ensure_index(recreate=True)
        es.index_docs(docs)
        r = evaluate(es, queries)
        out[analyzer] = (r["urdu"]["R@3"], r["urdu_variant"]["R@3"])
    assert out["standard"][1] <= out["standard"][0] - 0.15, out
    for robust in ("persian", "urdu_custom"):
        assert out[robust][1] >= out[robust][0] - 0.05, out


@needs_es
def test_make_search_uses_elasticsearch_when_it_is_reachable(docs, prefix):
    s = make_search(make_settings(elasticsearch_url=ES_URL, es_index_prefix=prefix), docs)
    assert isinstance(s, ResilientSearch) and s.name == "es-urdu_custom" and s.healthy()
    assert s.search("flu shot", 1)[0].doc.base_id == "service:vaccination" and s.fallbacks == 0


@needs_es
def test_search_keeps_working_when_the_cluster_disappears(docs):
    dead = ElasticSearchBackend("http://127.0.0.1:9", "urdu_custom", "nope")  # nothing listens on port 9
    s = ResilientSearch(dead, Bm25Search(docs))
    assert s.search("flu shot", 1)[0].doc.base_id == "service:vaccination" and s.fallbacks == 1


@needs_es
def test_call_transcripts_are_searchable_and_aggregate_like_the_python_oracle(rules, prefix, es_client):
    calls = synthetic_calls(rules, n=40, seed=3)
    ci = CallIndex(es_client, prefix)
    ci.ensure_index(recreate=True)
    assert ci.index_calls(calls) == 40 and ci.count() == 40
    es_ins, py_ins = ci.insights("handoff"), insights_in_python(calls, "handoff")
    assert es_ins["calls"] == py_ins["calls"] > 0 and es_ins["of_total"] == 40
    py_counts = {c["text"]: c["count"] for c in py_ins["top_last_utterances"]}
    for row in es_ins[
        "top_last_utterances"
    ]:  # the terms aggregation counts exact last utterances, so it must agree with Python
        assert py_counts.get(row["text"], row["count"]) == row["count"]
    assert es_ins["signal_terms"] and all(t["count"] >= 2 for t in es_ins["signal_terms"])
    said = py_ins["top_last_utterances"][0]["text"]  # something a caller really said in a handoff call
    hits = ci.search(said, outcome="handoff")
    assert hits and all(h["outcome"] == "handoff" for h in hits) and any(said in h["user_text"] for h in hits)


# ------------------------------------------------------------------------------------------------ Neo4j
@needs_neo
def test_syncing_the_graph_twice_changes_nothing(rules, docs, graph):
    spec = build_graph_spec(rules, docs)
    graph.sync(spec)
    first = graph.stats()
    graph.sync(spec)
    assert graph.stats() == first == InMemoryGraph(spec).stats()


@needs_neo
def test_cypher_and_the_in_memory_graph_give_identical_answers(rules, docs, graph):
    spec = build_graph_spec(rules, docs)
    graph.sync(spec)
    memory = InMemoryGraph(spec)
    refs = [ref_for(b) for b in sorted({d.base_id for d in docs})]
    assert graph.expand(refs) == memory.expand(refs)
    for s in rules.services:
        assert graph.policies_for_service(s.key) == memory.policies_for_service(s.key)
    for f in rules.faq:
        assert graph.services_for_faq(f.id) == memory.services_for_faq(f.id)


@needs_neo
def test_facts_removed_from_the_yaml_disappear_from_the_graph(rules, docs, graph):
    graph.sync(build_graph_spec(rules, docs))
    assert "FAQ:phone" in graph.expand([ref_for("faq:phone")])
    graph.sync(build_graph_spec(rules, [d for d in docs if d.base_id != "faq:phone"]))
    assert graph.expand([ref_for("faq:phone")]) == {} and graph.stats()["nodes"]["FAQ"] == 9


@needs_neo
def test_appointments_sync_without_personal_data_and_feed_analytics(svc, rules, docs, graph):
    graph.sync(build_graph_spec(rules, docs))
    rows = appointment_rows(svc.db)
    assert rows
    graph.sync_appointments(rows)
    n = graph._read("MATCH (a:Appointment) RETURN count(a) AS n")[0]["n"]
    keys = {k for r in graph._read("MATCH (p:Patient) RETURN keys(p) AS k") for k in r["k"]}
    assert n == len(rows) and keys <= {
        "id",
        "preferred_lang",
    }  # ids and a language only: no names, birth dates or phone numbers
    late = [dict(r, late_change=True) for r in rows[:3]]
    graph.sync_appointments(late + rows[3:])
    hot = graph.late_change_hotspots()
    assert (
        hot
        and sum(h["late_changes"] for h in hot) == 3
        and hot == sorted(hot, key=lambda h: (-h["late_changes"], h["service"]))
    )
    graph.sync_appointments(rows[:2])  # stale appointments and the patients left with none are removed
    assert graph._read("MATCH (a:Appointment) RETURN count(a) AS n")[0]["n"] == 2
    assert graph._read("MATCH (p:Patient) WHERE NOT (p)-[:HAS_APPOINTMENT]->() RETURN count(p) AS n")[0]["n"] == 0


@needs_neo
def test_make_graph_uses_neo4j_and_falls_back_when_it_is_unreachable(rules, docs, graph):
    spec = build_graph_spec(rules, docs)
    live = make_graph(make_settings(neo4j_uri=NEO_URI, neo4j_password=NEO_PASSWORD), spec)
    assert isinstance(live, ResilientGraph) and live.name == "neo4j" and live.stats()["nodes"]["Service"] == 6
    down = make_graph(make_settings(neo4j_uri="bolt://127.0.0.1:9", neo4j_password="x"), spec)
    assert isinstance(down, InMemoryGraph) and down.stats()["nodes"]["Service"] == 6


# ------------------------------------------------------------------------------------------------ the whole pipeline
@needs_es
@needs_neo
def test_the_pipeline_is_idempotent_self_healing_and_propagates_deletions(svc, rules, prefix, graph, es_client):
    settings = make_settings(
        elasticsearch_url=ES_URL, es_index_prefix=prefix, neo4j_uri=NEO_URI, neo4j_password=NEO_PASSWORD
    )
    first = ingest(settings, svc.db, rules)
    assert first.sql["added"] == 54 and first.elasticsearch["indexed"] == 54 and first.neo4j["nodes"] == 28
    second = ingest(settings, svc.db, rules)
    assert (
        second.sql["unchanged"] == 54
        and second.elasticsearch["indexed"] == 0
        and second.elasticsearch["already_current"] == 54
    )

    es_client.indices.delete(index=second.elasticsearch["index"])  # someone wipes the search index...
    healed = ingest(settings, svc.db, rules)
    assert healed.elasticsearch["indexed"] == 54  # ...and the next run rebuilds it from SQL

    rules.faq = [f for f in rules.faq if f.id != "phone"]  # the clinic drops a FAQ in the YAML
    gone = ingest(settings, svc.db, rules)
    assert gone.sql["removed"] == 2 and gone.elasticsearch["deleted"] == 2
    assert graph.stats()["nodes"]["FAQ"] == 9 and es_client.count(index=gone.elasticsearch["index"])["count"] == 52
