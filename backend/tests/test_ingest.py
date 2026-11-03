"""The ingestion pipeline's SQL side and its safety rails (no external services needed)."""

import pytest
from sqlalchemy import select

from app import models as m
from app.knowledge.docs import build_documents
from app.knowledge.ingest import IngestError, ingest, validate
from tests.conftest import make_settings


def run(svc, rules, **kw):
    return ingest(make_settings(), svc.db, rules, **kw)


def test_first_run_loads_everything_and_the_second_changes_nothing(svc, rules):
    assert run(svc, rules).sql == {"added": 54, "updated": 0, "unchanged": 0, "removed": 0}
    assert run(svc, rules).sql == {"added": 0, "updated": 0, "unchanged": 54, "removed": 0}
    assert svc.db.scalar(
        select(m.KnowledgeDoc).where(m.KnowledgeDoc.doc_id == "policy:cancel_notice@en")
    ).source.startswith("clinic.yaml")


def test_dry_run_reports_but_writes_nothing(svc, rules):
    report = run(svc, rules, dry_run=True)
    assert report.dry_run and report.sql["added"] == 54
    assert svc.db.scalars(select(m.KnowledgeDoc)).first() is None


def test_a_changed_policy_is_an_update_and_a_deleted_fact_is_removed(svc, rules):
    run(svc, rules)
    rules.policy.cancel_notice_hours = 48  # the clinic changes its rule in the YAML
    changed = run(svc, rules).sql
    assert (
        changed["updated"] == 2 and changed["added"] == 0
    )  # the English and Urdu cancellation policy (the FAQ answer is separate text)
    assert "48 hours" in svc.db.get(m.KnowledgeDoc, "policy:cancel_notice@en").text
    rules.faq = [f for f in rules.faq if f.id != "phone"]
    assert run(svc, rules).sql["removed"] == 2


def test_invalid_input_is_refused_with_every_problem_listed(rules, monkeypatch):
    import app.knowledge.ingest as ing

    monkeypatch.setattr(ing, "build_documents", lambda r: (_ for _ in ()).throw(ValueError("empty corpus")))
    with pytest.raises(IngestError) as e:
        validate(rules)
    assert e.value.problems == ["documents: empty corpus"]


def test_a_dangling_graph_reference_stops_ingestion_before_any_write(svc, rules, monkeypatch):
    import app.knowledge.ingest as ing

    real = build_documents(rules)
    bad = [d.model_copy(update={"refs": {"policies": ["ghost"]}}) if d.doc_id == "faq:hours@en" else d for d in real]
    monkeypatch.setattr(ing, "build_documents", lambda r: bad)
    with pytest.raises(IngestError, match="graph"):
        run(svc, rules)
    assert svc.db.scalars(select(m.KnowledgeDoc)).first() is None
