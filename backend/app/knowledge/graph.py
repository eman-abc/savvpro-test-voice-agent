"""The clinic knowledge graph.

SQL stays the source of truth for bookings (they need transactions: no double booking, atomic reschedule). The graph is a
read-optimised *knowledge layer* over the same facts: which providers offer which services, which policies govern them, and
which FAQ answers explain them. It is built from the YAML, so every node carries a `source` pointing back at where it came from.

Two interchangeable implementations of one interface:
- `InMemoryGraph`: pure Python over the same spec. The fallback (the demo works without Neo4j) and the test oracle.
- `Neo4jGraph`: Cypher over a real Neo4j. A parity test asserts both return identical answers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.knowledge.docs import KDoc
from app.rules import ClinicRules

log = logging.getLogger("knowledge.graph")

STATIC_LABELS = ("Clinic", "Provider", "Service", "Policy", "FAQ")
# The only relationship shapes the graph may contain. They are also the allow-list for the Cypher built below (labels and
# relationship types cannot be query parameters, so they are interpolated only from this fixed set).
REL_SHAPES = (
    ("Clinic", "EMPLOYS", "Provider"),
    ("Provider", "OFFERS", "Service"),
    ("Service", "GOVERNED_BY", "Policy"),
    ("FAQ", "ANSWERS", "Policy"),
    ("FAQ", "ANSWERS", "Service"),
)
KIND_TO_LABEL = {"faq": "FAQ", "policy": "Policy", "service": "Service", "provider": "Provider"}


def ref_for(base_id: str) -> dict[str, str]:
    kind, key = base_id.split(":", 1)
    return {"label": KIND_TO_LABEL[kind], "key": key}


@dataclass(frozen=True)
class Node:
    label: str
    key: str
    props: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


@dataclass(frozen=True)
class Edge:
    src_label: str
    rel: str
    dst_label: str
    src: str
    dst: str


@dataclass
class GraphSpec:
    nodes: list[Node]
    edges: list[Edge]


def build_graph_spec(rules: ClinicRules, docs: list[KDoc]) -> GraphSpec:
    by_lang: dict[tuple[str, str], KDoc] = {(d.base_id, d.lang): d for d in docs}
    nodes: list[Node] = [
        Node(
            "Clinic",
            "clinic",
            {
                "title": rules.clinic.name,
                "title_ur": rules.clinic.name_ur,
                "phone": rules.clinic.phone,
                "source": "clinic.yaml · clinic",
            },
        )
    ]
    edges: list[Edge] = []
    for p in rules.providers:
        nodes.append(
            Node(
                "Provider",
                p.key,
                {
                    "title": f"{p.title} {p.name}",
                    "title_ur": f"{p.title_ur} {p.name_ur}",
                    "specialty": p.specialty,
                    "source": f"clinic.yaml · providers.{p.key}",
                },
            )
        )
        edges.append(Edge("Clinic", "EMPLOYS", "Provider", "clinic", p.key))
        edges += [Edge("Provider", "OFFERS", "Service", p.key, s) for s in p.services]
    for s in rules.services:
        nodes.append(
            Node(
                "Service",
                s.key,
                {
                    "title": s.name.capitalize(),
                    "title_ur": s.name_ur,
                    "duration_minutes": s.duration_minutes,
                    "requires": s.requires,
                    "source": f"clinic.yaml · services.{s.key}",
                },
            )
        )
    for d in docs:
        if d.lang != "en" or d.kind not in ("policy", "faq"):
            continue
        key = d.base_id.split(":", 1)[1]
        ur = by_lang.get((d.base_id, "ur"), d)
        label = KIND_TO_LABEL[d.kind]
        nodes.append(
            Node(
                label,
                key,
                {"title": d.title, "title_ur": ur.title, "text": d.text, "text_ur": ur.text, "source": d.source},
            )
        )
        if d.kind == "policy":
            edges += [Edge("Service", "GOVERNED_BY", "Policy", s, key) for s in d.refs.get("services", [])]
        else:
            edges += [Edge("FAQ", "ANSWERS", "Policy", key, x) for x in d.refs.get("policies", [])]
            edges += [Edge("FAQ", "ANSWERS", "Service", key, x) for x in d.refs.get("services", [])]
    known = {(n.label, n.key) for n in nodes}
    problems = [
        f"{e.src_label}:{e.src} -{e.rel}-> {e.dst_label}:{e.dst}"
        for e in edges
        if (e.src_label, e.src) not in known
        or (e.dst_label, e.dst) not in known
        or (e.src_label, e.rel, e.dst_label) not in REL_SHAPES
    ]
    if problems:
        raise ValueError("graph references unknown nodes or relationship shapes: " + "; ".join(problems))
    if len(known) != len(nodes):
        raise ValueError("duplicate graph node keys")
    return GraphSpec(nodes, sorted(set(edges), key=lambda e: (e.src_label, e.rel, e.dst_label, e.src, e.dst)))


class KnowledgeGraph(Protocol):
    name: str

    def healthy(self) -> bool: ...
    def expand(self, refs: list[dict[str, str]]) -> dict[str, dict[str, Any]]: ...
    def policies_for_service(self, service_key: str) -> list[dict[str, Any]]: ...
    def services_for_faq(self, faq_key: str) -> list[dict[str, Any]]: ...
    def stats(self) -> dict[str, Any]: ...
    def late_change_hotspots(self, limit: int = 5) -> list[dict[str, Any]]: ...


def _node_view(n: Node) -> dict[str, Any]:
    return {"label": n.label, "key": n.key, "title": n.props.get("title"), "source": n.props.get("source")}


class InMemoryGraph:
    name = "in-memory"

    def __init__(self, spec: GraphSpec):
        self.spec = spec
        self.nodes = {(n.label, n.key): n for n in spec.nodes}

    def healthy(self) -> bool:
        return True

    def expand(self, refs: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for ref in refs:
            n = self.nodes.get((ref["label"], ref["key"]))
            if n is None:
                continue
            related: list[dict[str, Any]] = []
            for e in self.spec.edges:
                if (e.src_label, e.src) == (n.label, n.key):
                    other, direction = self.nodes[(e.dst_label, e.dst)], "out"
                elif (e.dst_label, e.dst) == (n.label, n.key):
                    other, direction = self.nodes[(e.src_label, e.src)], "in"
                else:
                    continue
                related.append({"direction": direction, "rel": e.rel, **_node_view(other)})
            related.sort(key=lambda r: (r["rel"], r["label"], r["key"]))
            out[f"{n.label}:{n.key}"] = {**_node_view(n), "text": n.props.get("text"), "related": related}
        return out

    def policies_for_service(self, service_key: str) -> list[dict[str, Any]]:
        keys = sorted(
            e.dst for e in self.spec.edges if (e.src_label, e.rel, e.src) == ("Service", "GOVERNED_BY", service_key)
        )
        return [
            {
                "key": k,
                "title": self.nodes[("Policy", k)].props["title"],
                "text": self.nodes[("Policy", k)].props["text"],
                "source": self.nodes[("Policy", k)].props["source"],
            }
            for k in keys
        ]

    def services_for_faq(self, faq_key: str) -> list[dict[str, Any]]:
        out = []
        for s in sorted(
            e.dst
            for e in self.spec.edges
            if (e.src_label, e.rel, e.dst_label, e.src) == ("FAQ", "ANSWERS", "Service", faq_key)
        ):
            providers = sorted(e.src for e in self.spec.edges if (e.rel, e.dst) == ("OFFERS", s))
            out.append(
                {
                    "key": s,
                    "title": self.nodes[("Service", s)].props["title"],
                    "providers": [{"key": p, "title": self.nodes[("Provider", p)].props["title"]} for p in providers],
                }
            )
        return out

    def stats(self) -> dict[str, Any]:
        nodes: dict[str, int] = {}
        rels: dict[str, int] = {}
        for n in self.spec.nodes:
            nodes[n.label] = nodes.get(n.label, 0) + 1
        for e in self.spec.edges:
            rels[e.rel] = rels.get(e.rel, 0) + 1
        return {"nodes": dict(sorted(nodes.items())), "relationships": dict(sorted(rels.items()))}

    def late_change_hotspots(self, limit: int = 5) -> list[dict[str, Any]]:
        return []  # appointment history lives only in the Neo4j projection


class Neo4jGraph:
    name = "neo4j"

    def __init__(self, uri: str, user: str, password: str, driver: Any = None):
        if driver is None:
            from neo4j import GraphDatabase

            driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=5)
        self.driver = driver

    def close(self) -> None:
        self.driver.close()

    def _read(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        with self.driver.session() as s:
            return [dict(r) for r in s.run(cypher, **params)]

    def healthy(self) -> bool:
        try:
            self._read("RETURN 1 AS ok")
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_constraints(self) -> None:
        with self.driver.session() as s:
            for label in (*STATIC_LABELS,):
                s.run(
                    f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.key IS UNIQUE"
                ).consume()
            for label in ("Patient", "Appointment"):
                s.run(
                    f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:`{label}`) REQUIRE n.id IS UNIQUE"
                ).consume()

    def sync(self, spec: GraphSpec) -> dict[str, int]:
        """Idempotent: MERGE everything in the spec, then remove what is no longer in it, in one transaction."""
        self.ensure_constraints()

        def work(tx: Any) -> dict[str, int]:
            for label in STATIC_LABELS:
                rows = [{"key": n.key, "props": n.props} for n in spec.nodes if n.label == label]
                tx.run(
                    f"UNWIND $rows AS r MERGE (n:`{label}` {{key: r.key}}) SET n = r.props SET n.key = r.key", rows=rows
                )
                tx.run(f"MATCH (n:`{label}`) WHERE NOT n.key IN $keys DETACH DELETE n", keys=[r["key"] for r in rows])
            for src, rel, dst in REL_SHAPES:
                tx.run(f"MATCH (:`{src}`)-[r:`{rel}`]->(:`{dst}`) DELETE r")
                rows = [
                    {"src": e.src, "dst": e.dst}
                    for e in spec.edges
                    if (e.src_label, e.rel, e.dst_label) == (src, rel, dst)
                ]
                tx.run(
                    f"UNWIND $rows AS r MATCH (a:`{src}` {{key: r.src}}) MATCH (b:`{dst}` {{key: r.dst}}) MERGE (a)-[:`{rel}`]->(b)",
                    rows=rows,
                )
            return {"nodes": len(spec.nodes), "relationships": len(spec.edges)}

        with self.driver.session() as s:
            return s.execute_write(work)

    def sync_appointments(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Read-model of bookings for analytics. Patients are ids only: no names, birth dates or phone numbers."""

        def work(tx: Any) -> None:
            tx.run("MATCH (a:Appointment)-[r:WITH|FOR]->() DELETE r")
            tx.run("MATCH (:Patient)-[r:HAS_APPOINTMENT]->(:Appointment) DELETE r")
            tx.run(
                """UNWIND $rows AS r
                      MERGE (p:Patient {id: r.patient_id}) SET p.preferred_lang = r.lang
                      MERGE (a:Appointment {id: r.id}) SET a.start = r.start, a.status = r.status, a.late_change = r.late_change, a.created_via = r.via
                      MERGE (p)-[:HAS_APPOINTMENT]->(a)
                      WITH a, r
                      MATCH (pr:Provider {key: r.provider}) MERGE (a)-[:WITH]->(pr)
                      WITH a, r
                      MATCH (s:Service {key: r.service}) MERGE (a)-[:FOR]->(s)""",
                rows=rows,
            )
            tx.run("MATCH (a:Appointment) WHERE NOT a.id IN $ids DETACH DELETE a", ids=[r["id"] for r in rows])
            tx.run("MATCH (p:Patient) WHERE NOT (p)-[:HAS_APPOINTMENT]->() DELETE p")

        with self.driver.session() as s:
            s.execute_write(work)
        return {"appointments": len(rows)}

    def expand(self, refs: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        rows = self._read(
            """UNWIND $refs AS ref
               MATCH (n) WHERE n.key = ref.key AND ref.label IN labels(n)
               OPTIONAL MATCH (n)-[r]-(m) WHERE any(l IN labels(m) WHERE l IN $static)
               RETURN ref.label AS label, n.key AS key, n.title AS title, n.source AS source, n.text AS text,
                      CASE WHEN r IS NULL THEN null WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction,
                      type(r) AS rel, labels(m)[0] AS mlabel, m.key AS mkey, m.title AS mtitle, m.source AS msource""",
            refs=refs,
            static=list(STATIC_LABELS),
        )
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            entry = out.setdefault(
                f"{r['label']}:{r['key']}",
                {
                    "label": r["label"],
                    "key": r["key"],
                    "title": r["title"],
                    "source": r["source"],
                    "text": r["text"],
                    "related": [],
                },
            )
            if r["rel"]:
                entry["related"].append(
                    {
                        "direction": r["direction"],
                        "rel": r["rel"],
                        "label": r["mlabel"],
                        "key": r["mkey"],
                        "title": r["mtitle"],
                        "source": r["msource"],
                    }
                )
        for e in out.values():
            e["related"].sort(key=lambda x: (x["rel"], x["label"], x["key"]))
        return out

    def policies_for_service(self, service_key: str) -> list[dict[str, Any]]:
        return self._read(
            "MATCH (:Service {key: $k})-[:GOVERNED_BY]->(p:Policy) RETURN p.key AS key, p.title AS title, p.text AS text, p.source AS source ORDER BY key",
            k=service_key,
        )

    def services_for_faq(self, faq_key: str) -> list[dict[str, Any]]:
        rows = self._read(
            """MATCH (:FAQ {key: $k})-[:ANSWERS]->(s:Service)
                             OPTIONAL MATCH (p:Provider)-[:OFFERS]->(s)
                             WITH s, p ORDER BY p.key
                             RETURN s.key AS key, s.title AS title, collect(CASE WHEN p IS NULL THEN null ELSE {key: p.key, title: p.title} END) AS providers
                             ORDER BY key""",
            k=faq_key,
        )
        return [{**r, "providers": [p for p in r["providers"] if p]} for r in rows]

    def stats(self) -> dict[str, Any]:
        nodes = {
            r["l"]: r["c"]
            for r in self._read(
                "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $static) RETURN labels(n)[0] AS l, count(*) AS c ORDER BY l",
                static=list(STATIC_LABELS),
            )
        }
        rels = {
            r["t"]: r["c"]
            for r in self._read(
                "MATCH (a)-[r]->(b) WHERE any(l IN labels(a) WHERE l IN $static) AND any(l IN labels(b) WHERE l IN $static) RETURN type(r) AS t, count(*) AS c ORDER BY t",
                static=list(STATIC_LABELS),
            )
        }
        return {"nodes": nodes, "relationships": rels}

    def late_change_hotspots(self, limit: int = 5) -> list[dict[str, Any]]:
        return self._read(
            """MATCH (a:Appointment {late_change: true})-[:FOR]->(s:Service)
                             RETURN s.key AS service, s.title AS title, count(a) AS late_changes ORDER BY late_changes DESC, service LIMIT $limit""",
            limit=limit,
        )


class ResilientGraph:
    """Neo4j when it answers, the in-memory graph when it does not."""

    def __init__(self, primary: Neo4jGraph, fallback: InMemoryGraph):
        self.primary, self.fallback, self.name = primary, fallback, primary.name
        self.fallbacks = 0

    def healthy(self) -> bool:
        return self.primary.healthy()

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.primary, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001
            self.fallbacks += 1
            log.warning("graph backend failed in %s; using the in-memory graph", method, exc_info=self.fallbacks == 1)
            return getattr(self.fallback, method)(*args, **kwargs)

    def expand(self, refs: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        return self._call("expand", refs)

    def policies_for_service(self, service_key: str) -> list[dict[str, Any]]:
        return self._call("policies_for_service", service_key)

    def services_for_faq(self, faq_key: str) -> list[dict[str, Any]]:
        return self._call("services_for_faq", faq_key)

    def stats(self) -> dict[str, Any]:
        return self._call("stats")

    def late_change_hotspots(self, limit: int = 5) -> list[dict[str, Any]]:
        return self._call("late_change_hotspots", limit)


def make_graph(settings: Settings, spec: GraphSpec) -> KnowledgeGraph:
    memory = InMemoryGraph(spec)
    if not settings.neo4j_uri or not settings.neo4j_password:
        return memory
    try:
        g = Neo4jGraph(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        if not g.healthy():
            raise ConnectionError("Neo4j did not answer")
        g.sync(spec)  # idempotent
        return ResilientGraph(g, memory)
    except Exception:  # noqa: BLE001
        log.warning("Neo4j is configured but unavailable; using the in-memory graph", exc_info=True)
        return memory
