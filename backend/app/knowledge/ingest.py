"""One ingestion pipeline: YAML + FAQ + transcripts  ->  SQL, Elasticsearch and Neo4j.

    python -m app.knowledge.ingest [--dry-run] [--calls 300]

- **Validated**: documents are typed (pydantic) and the graph is checked for dangling references before anything is written.
- **Idempotent**: documents are fingerprinted; running it twice changes nothing. SQL remembers what was loaded, and the
  Elasticsearch / Neo4j projections are reconciled against it, so a wiped index or graph heals on the next run.
- **Observable**: it returns a report of what was added, updated, unchanged and removed per target; `--dry-run` writes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.config import Settings
from app.db import Database
from app.knowledge.docs import KDoc, build_documents
from app.knowledge.graph import Neo4jGraph, build_graph_spec
from app.knowledge.search import ElasticSearchBackend
from app.knowledge.transcripts import CallIndex, calls_from_sql, synthetic_calls
from app.rules import ClinicRules, load_rules

log = logging.getLogger("knowledge.ingest")


class IngestError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class IngestReport:
    dry_run: bool
    documents: int = 0
    sql: dict[str, int] = field(default_factory=dict)
    elasticsearch: dict[str, Any] | None = None
    neo4j: dict[str, Any] | None = None
    calls: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate(rules: ClinicRules) -> tuple[list[KDoc], Any]:
    problems: list[str] = []
    docs: list[KDoc] = []
    spec = None
    try:
        docs = build_documents(rules)
    except Exception as exc:  # noqa: BLE001 - report every kind of invalid input the same way
        problems.append(f"documents: {exc}")
    if docs:
        try:
            spec = build_graph_spec(rules, docs)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"graph: {exc}")
    if problems:
        raise IngestError(problems)
    return docs, spec


def appointment_rows(db: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": a.id,
            "patient_id": a.patient_id,
            "lang": a.patient.preferred_lang,
            "provider": a.provider.key,
            "service": a.service_key,
            "start": a.start_at.isoformat(),
            "status": a.status,
            "late_change": a.late_change,
            "via": a.created_via,
        }
        for a in db.scalars(select(m.Appointment))
    ]


def ingest(
    settings: Settings,
    db: Session,
    rules: ClinicRules,
    *,
    dry_run: bool = False,
    calls: int = 0,
    es: ElasticSearchBackend | None = None,
    graph: Neo4jGraph | None = None,
    call_index: CallIndex | None = None,
) -> IngestReport:
    docs, spec = validate(rules)
    report = IngestReport(dry_run=dry_run, documents=len(docs))

    # ---- SQL: what changed since the last load?
    existing = {r.doc_id: r.checksum for r in db.scalars(select(m.KnowledgeDoc))}
    current = {d.doc_id: d for d in docs}
    added = [i for i in current if i not in existing]
    updated = [i for i in current if i in existing and existing[i] != current[i].checksum]
    removed = [i for i in existing if i not in current]
    report.sql = {
        "added": len(added),
        "updated": len(updated),
        "unchanged": len(current) - len(added) - len(updated),
        "removed": len(removed),
    }
    if not dry_run:
        for i in added + updated:
            d = current[i]
            row = db.get(m.KnowledgeDoc, i) or m.KnowledgeDoc(doc_id=i)
            row.base_id, row.kind, row.lang, row.checksum, row.title, row.text, row.source = (
                d.base_id,
                d.kind,
                d.lang,
                d.checksum,
                d.title,
                d.text,
                d.source,
            )
            row.updated_at = m.utcnow()
            db.add(row)
        for i in removed:
            db.delete(db.get(m.KnowledgeDoc, i))
        db.commit()

    # ---- Elasticsearch: reconcile the index with SQL (self-healing if the index was wiped)
    if es is None and settings.elasticsearch_url:
        es = ElasticSearchBackend(
            settings.elasticsearch_url,
            settings.es_urdu_analyzer,
            settings.es_index_prefix,
            settings.elasticsearch_api_key,
        )
    if es is not None:
        es_ids = set(es.indexed_ids())
        to_index = [d for i, d in current.items() if i in set(added) | set(updated) or i not in es_ids]
        to_delete = sorted(es_ids - set(current))
        report.elasticsearch = {
            "index": es.index,
            "indexed": len(to_index),
            "deleted": len(to_delete),
            "already_current": len(current) - len(to_index),
        }
        if not dry_run:
            es.ensure_index()
            es.index_docs(to_index)
            es.delete_ids(to_delete)

    # ---- Neo4j: MERGE is idempotent, so a full sync every time is safe
    if graph is None and settings.neo4j_uri and settings.neo4j_password:
        graph = Neo4jGraph(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    if graph is not None:
        rows = appointment_rows(db)
        report.neo4j = {"nodes": len(spec.nodes), "relationships": len(spec.edges), "appointments": len(rows)}
        if not dry_run:
            graph.sync(spec)
            graph.sync_appointments(rows)

    # ---- transcripts: real calls from SQL, plus synthetic ones if asked
    if es is not None and (calls or call_index is not None):
        ci = call_index or CallIndex(es.es, settings.es_index_prefix)
        batch = calls_from_sql(db) + (synthetic_calls(rules, calls) if calls else [])
        report.calls = {"index": ci.index, "documents": len(batch), "synthetic": calls}
        if not dry_run and batch:
            ci.index_calls(batch)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="validate and report; write nothing")
    ap.add_argument(
        "--calls", type=int, default=0, help="also index N synthetic call transcripts (needs Elasticsearch)"
    )
    ap.add_argument("--rules", default="config/clinic.yaml")
    args = ap.parse_args()
    settings = Settings()
    db = Database(settings)
    from app.models import Base

    Base.metadata.create_all(db.engine)  # in production run the Alembic migrations first
    try:
        with db.session() as session:
            report = ingest(settings, session, load_rules(args.rules), dry_run=args.dry_run, calls=args.calls)
    except IngestError as exc:
        print("INGEST REFUSED, nothing was written:")
        for p in exc.problems:
            print("  -", p)
        raise SystemExit(1) from exc
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
