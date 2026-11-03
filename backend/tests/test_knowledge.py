"""Knowledge documents, search backends, fusion, fallback, the graph (in memory) and GraphRAG assembly. No external services needed."""

import pytest

from app.knowledge.docs import FAQ_REFS, KDoc, build_documents
from app.knowledge.graph import REL_SHAPES, InMemoryGraph, build_graph_spec, ref_for
from app.knowledge.rag import KnowledgeService, describe_related
from app.knowledge.search import (
    Bm25Search,
    DenseSearch,
    Hit,
    HybridSearch,
    ResilientSearch,
    best_per_fact,
    fold_urdu,
    make_search,
    tokenize,
)
from tests.conftest import make_settings


@pytest.fixture
def docs(rules):
    return build_documents(rules)


@pytest.fixture
def knowledge(rules, docs):
    return KnowledgeService(docs, Bm25Search(docs), InMemoryGraph(build_graph_spec(rules, docs)))


# ------------------------------------------------------------------------------------------------ documents
def test_documents_cover_every_fact_in_both_languages(docs):
    kinds = {(d.kind, d.lang) for d in docs}
    assert kinds == {(k, lang) for k in ("faq", "policy", "service", "provider") for lang in ("en", "ur")}
    assert len(docs) == 54 and len({d.base_id for d in docs}) == 27
    assert len({d.doc_id for d in docs}) == len(docs)  # ids are unique


def test_every_document_names_its_source(docs):
    assert all(d.source.startswith("clinic.yaml · ") for d in docs)


def test_policy_numbers_come_from_the_yaml_not_from_the_code(rules, docs):
    cancel = next(d for d in docs if d.doc_id == "policy:cancel_notice@en")
    assert f"{rules.policy.cancel_notice_hours} hours" in cancel.text
    ur = next(d for d in docs if d.doc_id == "policy:cancel_notice@ur")
    assert str(rules.policy.cancel_notice_hours) in ur.text and any("؀" <= c <= "ۿ" for c in ur.text)


def test_urdu_documents_are_in_urdu_script(docs):
    for d in docs:
        if d.lang == "ur":
            assert any("؀" <= c <= "ۿ" for c in d.text), d.doc_id


def test_checksum_is_stable_and_changes_with_content(docs):
    d = docs[0]
    assert d.checksum == KDoc(**d.model_dump()).checksum
    assert d.checksum != KDoc(**{**d.model_dump(), "text": d.text + " Changed."}).checksum


def test_blank_fields_are_rejected():
    with pytest.raises(ValueError):
        KDoc(doc_id="x@en", base_id="x", kind="faq", lang="en", title="  ", text="t", source="s")


def test_faq_refs_only_point_at_things_that_exist(rules):
    pol = {
        "cancel_notice",
        "late_arrival",
        "arrive_early",
        "min_lead",
        "max_advance",
        "verification",
        "holidays",
        "emergency",
    }
    for refs in FAQ_REFS.values():
        assert set(refs.get("policies", [])) <= pol
        assert set(refs.get("services", [])) <= {s.key for s in rules.services} | {"*"}


# ------------------------------------------------------------------------------------------------ BM25 and Urdu folding
def test_bm25_finds_the_obvious_answer(docs):
    s = Bm25Search(docs)
    assert s.search("can I cancel for free", 3)[0].doc.base_id in ("faq:cancellation", "policy:cancel_notice")
    assert s.search("flu shot", 1)[0].doc.base_id == "service:vaccination"
    assert s.search("zzzz qqqq", 3) == []


def test_bm25_filters_by_language_and_kind(docs):
    s = Bm25Search(docs)
    assert {h.doc.lang for h in s.search("cancellation", 10, lang="en")} == {"en"}
    assert {h.doc.kind for h in s.search("cancellation", 10, kind="policy")} <= {"policy"}


def test_urdu_letter_variants_fold_together():
    assert fold_urdu("كيا") == fold_urdu("کیا")  # Arabic kaf/yeh vs Urdu kaf/yeh
    assert fold_urdu("كِتَابٌ") == "کتاب"  # vowel marks removed
    assert (
        tokenize("كهاں") == tokenize("کہاں") == ["کہاں"]
    )  # Arabic kaf/heh typed on an Arabic keyboard fold to the Urdu forms
    assert "کا" not in tokenize("کلینک کا پتہ")  # Urdu stop words are dropped


def test_a_keyboard_variant_query_finds_the_same_document(docs):
    s = Bm25Search(docs)
    clean_query = "کلینک کہاں ہے"
    variant = clean_query.translate(str.maketrans({"ی": "ي", "ک": "ك", "ہ": "ه"}))
    assert variant != clean_query
    expected = [h.doc.doc_id for h in s.search(clean_query, 3)]
    assert expected and [h.doc.doc_id for h in s.search(variant, 3)] == expected


def test_best_per_fact_keeps_one_document_per_fact(docs):
    hits = [
        Hit(d, 1.0, i)
        for i, d in enumerate(
            [x for x in docs if x.base_id == "faq:hours"] + [x for x in docs if x.base_id == "faq:phone"][:1], 1
        )
    ]
    out = best_per_fact(hits)
    assert [h.doc.base_id for h in out] == ["faq:hours", "faq:phone"] and [h.rank for h in out] == [1, 2]


# ------------------------------------------------------------------------------------------------ fusion, fallback, factory
class Stub:
    def __init__(self, name, ids, docs):
        self.name, self.ids, self.by_id = name, ids, {d.doc_id: d for d in docs}

    def healthy(self):
        return True

    def search(self, query, k=5, lang=None, kind=None):
        return [Hit(self.by_id[i], 1.0, r) for r, i in enumerate(self.ids[:k], 1)]


def test_reciprocal_rank_fusion_rewards_agreement(docs):
    a = Stub("a", ["faq:hours@en", "faq:phone@en", "faq:parking@en"], docs)
    b = Stub("b", ["faq:parking@en", "faq:hours@en", "faq:bring@en"], docs)
    out = HybridSearch(a, b).search("x", 4)
    assert [h.doc.doc_id for h in out][:2] == [
        "faq:hours@en",
        "faq:parking@en",
    ]  # both lists contain them, so they outrank single-list hits
    assert out[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert HybridSearch(a, b).name == "hybrid(a+b)"


class Broken:
    name = "broken"

    def healthy(self):
        return False

    def search(self, *a, **k):
        raise ConnectionError("cluster down")


def test_search_falls_back_to_bm25_when_the_primary_fails(docs):
    r = ResilientSearch(Broken(), Bm25Search(docs))
    assert r.search("flu shot", 1)[0].doc.base_id == "service:vaccination"
    assert r.fallbacks == 1 and r.name == "broken" and not r.healthy()


def test_make_search_is_plain_bm25_without_elasticsearch(docs):
    assert make_search(make_settings(), docs).name == "bm25-own"


def test_make_search_survives_an_unreachable_elasticsearch(docs):
    s = make_search(make_settings(elasticsearch_url="http://127.0.0.1:9"), docs)  # nothing listens on port 9
    assert s.name == "bm25-own" and s.search("flu shot", 1)


def test_dense_search_ranks_by_cosine_and_respects_filters(docs):
    import numpy as np

    vocab = sorted({w for d in docs for w in tokenize(f"{d.title} {d.text}")})

    def embed(texts):  # a toy bag-of-words embedder: enough to test the plumbing without downloading a model
        out = np.zeros((len(texts), len(vocab)))
        for i, t in enumerate(texts):
            for w in tokenize(fold_urdu(t)):
                if w in vocab:
                    out[i, vocab.index(w)] += 1
        return out

    s = DenseSearch(docs, embedder=embed)
    assert s.search("flu shot vaccination", 1)[0].doc.base_id == "service:vaccination"
    assert {h.doc.lang for h in s.search("cancel", 5, lang="ur")} <= {"ur"}


# ------------------------------------------------------------------------------------------------ graph (in memory)
def test_graph_spec_counts_and_sources(rules, docs):
    spec = build_graph_spec(rules, docs)
    assert len(spec.nodes) == 28 and len(spec.edges) == 58
    assert all(n.props.get("source") for n in spec.nodes)
    assert {(e.src_label, e.rel, e.dst_label) for e in spec.edges} <= set(REL_SHAPES)


def test_graph_spec_rejects_dangling_references(rules, docs):
    bad = [
        d.model_copy(update={"refs": {"policies": ["no_such_policy"]}}) if d.doc_id == "faq:hours@en" else d
        for d in docs
    ]
    with pytest.raises(ValueError, match="unknown nodes"):
        build_graph_spec(rules, bad)


def test_graph_spec_rejects_duplicate_nodes(rules, docs):
    first_faq = next(d for d in docs if d.kind == "faq" and d.lang == "en")
    again = first_faq.model_copy(update={"doc_id": first_faq.doc_id + "-dup"})  # the same fact twice
    with pytest.raises(ValueError, match="duplicate"):
        build_graph_spec(rules, [*docs, again])


def test_policies_for_a_service_come_with_their_source(rules, docs):
    g = InMemoryGraph(build_graph_spec(rules, docs))
    ps = g.policies_for_service("vaccination")
    assert {p["key"] for p in ps} >= {"cancel_notice", "late_arrival"}
    cancel = next(p for p in ps if p["key"] == "cancel_notice")
    assert cancel["source"] == "clinic.yaml · policy.cancel_notice_hours" and "24 hours" in cancel["text"]


def test_a_faq_suggests_the_right_service_and_who_offers_it(rules, docs):
    g = InMemoryGraph(build_graph_spec(rules, docs))
    [s] = g.services_for_faq("new_patients")
    assert s["key"] == "new_patient_visit" and {p["key"] for p in s["providers"]} == {"patel", "nguyen"}


def test_expand_describes_neighbours_in_both_directions(rules, docs):
    g = InMemoryGraph(build_graph_spec(rules, docs))
    e = g.expand([ref_for("faq:cancellation"), ref_for("policy:cancel_notice")])
    assert any(r["rel"] == "ANSWERS" and r["key"] == "cancel_notice" for r in e["FAQ:cancellation"]["related"])
    assert any(r["direction"] == "in" and r["rel"] == "GOVERNED_BY" for r in e["Policy:cancel_notice"]["related"])
    assert any("Applies to" in f for f in describe_related(e["Policy:cancel_notice"]))
    assert any("Backed by policy" in f for f in describe_related(e["FAQ:cancellation"]))


def test_unknown_references_are_ignored_not_fatal(rules, docs):
    assert InMemoryGraph(build_graph_spec(rules, docs)).expand([{"label": "Service", "key": "nope"}]) == {}


# ------------------------------------------------------------------------------------------------ GraphRAG assembly
def test_ask_returns_provenance_facts_and_a_suggested_service(knowledge):
    res = knowledge.ask("are you accepting new patients", "en")
    assert res["provenance"] and all(p.startswith("clinic.yaml") for p in res["provenance"])
    assert res["results"][0]["facts"] is not None
    assert any(
        s["service"] == "new_patient_visit" for s in res["suggestions"]
    )  # "suggest the right service after an FAQ"
    assert res["search_backend"] == "bm25-own" and res["graph_backend"] == "in-memory"


def test_ask_answers_in_the_callers_language(knowledge):
    en = knowledge.ask("what are your opening hours", "en")["results"][0]
    ur = knowledge.ask("what are your opening hours", "ur")["results"][0]
    assert en["id"] == ur["id"] and en["text"] != ur["text"] and any("؀" <= c <= "ۿ" for c in ur["text"])


def test_policies_for_explains_where_each_rule_comes_from(knowledge):
    out = knowledge.policies_for("annual_physical", "ur")
    assert out["policies"] and all(p["source"].startswith("clinic.yaml") for p in out["policies"])
    assert any("؀" <= c <= "ۿ" for c in out["policies"][0]["text"])


def test_status_reports_what_is_running(knowledge):
    st = knowledge.status()
    assert st["documents"] == 54 and st["facts"] == 27 and st["graph"]["nodes"]["Service"] == 6
