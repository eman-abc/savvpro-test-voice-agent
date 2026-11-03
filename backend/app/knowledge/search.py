"""Search over the knowledge documents. One interface, several interchangeable backends:

- `Bm25Search`: our own BM25. No dependencies; it is the fallback, so the deployed demo works with nothing else running.
- `ElasticSearchBackend`: Elasticsearch with per-language analyzers (Urdu has no built-in analyzer; see `ES_ANALYZERS`).
- `DenseSearch`: multilingual sentence embeddings (cosine similarity), computed in-process.
- `HybridSearch`: reciprocal-rank fusion of any two backends.
- `ResilientSearch`: tries the primary backend and falls back to BM25 when it fails.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from app.brain.urdu import _DIACRITICS, _VARIANTS, fold_urdu
from app.config import Settings
from app.knowledge.docs import KDoc

log = logging.getLogger("knowledge.search")


@dataclass
class Hit:
    doc: KDoc
    score: float
    rank: int  # 1-based


class SearchBackend(Protocol):
    name: str

    def search(self, query: str, k: int = 5, lang: str | None = None, kind: str | None = None) -> list[Hit]: ...

    def healthy(self) -> bool: ...


def best_per_fact(hits: list[Hit]) -> list[Hit]:
    """The same fact exists in both languages: keep the best-ranked document per fact."""
    seen: set[str] = set()
    out: list[Hit] = []
    for h in hits:
        if h.doc.base_id not in seen:
            seen.add(h.doc.base_id)
            out.append(Hit(h.doc, h.score, len(out) + 1))
    return out


# ------------------------------------------------------------------------------------------------ own BM25
EN_STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "do",
    "does",
    "i",
    "you",
    "to",
    "of",
    "my",
    "me",
    "can",
    "what",
    "how",
    "your",
    "we",
    "it",
    "in",
    "on",
    "for",
    "and",
    "or",
}
UR_STOP = {
    "کا",
    "کی",
    "کے",
    "ہے",
    "ہیں",
    "میں",
    "سے",
    "کو",
    "اور",
    "پر",
    "یہ",
    "وہ",
    "کر",
    "کیا",
    "ہو",
    "تھا",
    "تھی",
    "کہ",
    "ہوں",
    "ہم",
    "آپ",
    "مجھے",
    "ہمیں",
    "بھی",
    "تک",
}


def tokenize(text: str) -> list[str]:
    t = _DIACRITICS.sub("", text.lower()).translate(_VARIANTS)
    return [w for w in re.findall(r"\w+", t) if w not in EN_STOP and w not in UR_STOP]


class Bm25Search:
    name = "bm25-own"

    def __init__(self, docs: list[KDoc], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.tf = [Counter(tokenize(f"{d.title} {d.title} {' '.join(d.keywords)} {d.text}")) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = sum(self.len) / max(len(self.len), 1)
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log((n - f + 0.5) / (f + 0.5) + 1) for t, f in df.items()}

    def healthy(self) -> bool:
        return True

    def search(self, query: str, k: int = 5, lang: str | None = None, kind: str | None = None) -> list[Hit]:
        q = {t for t in tokenize(query) if t in self.idf}
        scored: list[tuple[float, int]] = []
        for i, d in enumerate(self.docs):
            if (lang and d.lang != lang) or (kind and d.kind != kind):
                continue
            s = 0.0
            for t in q:
                f = self.tf[i].get(t, 0)
                if f:
                    s += (
                        self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
                    )
            if s > 0:
                scored.append((s, i))
        scored.sort(key=lambda x: -x[0])
        return [Hit(self.docs[i], s, r) for r, (s, i) in enumerate(scored[:k], 1)]


# ------------------------------------------------------------------------------------------------ Elasticsearch
URDU_STOPWORDS = sorted(UR_STOP)
# Urdu has no built-in Elasticsearch analyzer, so the Urdu text field is analysed one of four ways and the variants are compared
# in the retrieval evaluation. "urdu_custom" folds Arabic-script letter variants into Urdu forms, strips vowel marks, maps
# Arabic-Indic digits to ASCII and removes Urdu stop words.
ES_ANALYZERS = ("standard", "arabic", "persian", "urdu_custom")


def es_settings() -> dict[str, Any]:
    return {
        "analysis": {
            "char_filter": {
                "urdu_chars": {
                    "type": "mapping",
                    "mappings": ["ي=>ی", "ى=>ی", "ك=>ک", "ة=>ہ", "ۃ=>ہ", "ۂ=>ہ", "ه=>ہ"]
                    + [f"\\u{c:04X}=>" for c in (0x64B, 0x64C, 0x64D, 0x64E, 0x64F, 0x650, 0x651, 0x652, 0x670, 0x640)],
                }
            },
            "filter": {"urdu_stop": {"type": "stop", "stopwords": URDU_STOPWORDS}},
            "analyzer": {
                "urdu_custom": {
                    "type": "custom",
                    "char_filter": ["urdu_chars"],
                    "tokenizer": "standard",
                    "filter": ["lowercase", "decimal_digit", "urdu_stop"],
                }
            },
        }
    }


def es_mappings(urdu_analyzer: str) -> dict[str, Any]:
    return {
        "properties": {
            "doc_id": {"type": "keyword"},
            "base_id": {"type": "keyword"},
            "kind": {"type": "keyword"},
            "lang": {"type": "keyword"},
            "source": {"type": "keyword"},
            "title": {"type": "text", "index": False},
            "text": {"type": "text", "index": False},
            "keywords": {"type": "keyword"},
            "refs": {"type": "object", "enabled": False},
            "title_en": {"type": "text", "analyzer": "english"},
            "text_en": {"type": "text", "analyzer": "english"},
            "keywords_en": {"type": "text", "analyzer": "english"},
            "title_ur": {"type": "text", "analyzer": urdu_analyzer},
            "text_ur": {"type": "text", "analyzer": urdu_analyzer},
        }
    }


def es_source(d: KDoc) -> dict[str, Any]:
    body = d.model_dump()
    body[f"title_{d.lang}"] = d.title
    body[f"text_{d.lang}"] = d.text
    if d.lang == "en":
        body["keywords_en"] = " ".join(d.keywords)
    return body


class ElasticSearchBackend:
    def __init__(
        self,
        url: str,
        urdu_analyzer: str = "urdu_custom",
        prefix: str = "clinic",
        api_key: str | None = None,
        client: Any = None,
    ):
        if urdu_analyzer not in ES_ANALYZERS:
            raise ValueError(f"urdu analyzer must be one of {ES_ANALYZERS}")
        if client is None:
            from elasticsearch import Elasticsearch

            client = Elasticsearch(url, api_key=api_key, request_timeout=10, max_retries=1)
        self.es, self.analyzer = client, urdu_analyzer
        self.index = f"{prefix}_kb_{urdu_analyzer}"
        self.name = f"es-{urdu_analyzer}"

    def healthy(self) -> bool:
        try:
            return bool(self.es.ping())
        except Exception:  # noqa: BLE001
            return False

    def ensure_index(self, recreate: bool = False) -> None:
        if recreate and self.es.indices.exists(index=self.index):
            self.es.indices.delete(index=self.index)
        if not self.es.indices.exists(index=self.index):
            self.es.indices.create(index=self.index, settings=es_settings(), mappings=es_mappings(self.analyzer))

    def index_docs(self, docs: list[KDoc]) -> int:
        from elasticsearch import helpers

        self.ensure_index()
        actions = [{"_index": self.index, "_id": d.doc_id, "_source": es_source(d)} for d in docs]
        ok, _ = helpers.bulk(self.es, actions, refresh="wait_for")
        return int(ok)

    def delete_ids(self, ids: list[str]) -> int:
        from elasticsearch import helpers

        if not ids:
            return 0
        ok, _ = helpers.bulk(
            self.es,
            [{"_op_type": "delete", "_index": self.index, "_id": i} for i in ids],
            refresh="wait_for",
            raise_on_error=False,
        )
        return int(ok)

    def indexed_ids(self) -> list[str]:
        if not self.es.indices.exists(index=self.index):
            return []
        res = self.es.search(index=self.index, query={"match_all": {}}, size=10000, _source=False)
        return [h["_id"] for h in res["hits"]["hits"]]

    def search(self, query: str, k: int = 5, lang: str | None = None, kind: str | None = None) -> list[Hit]:
        filters: list[dict[str, Any]] = []
        if lang:
            filters.append({"term": {"lang": lang}})
        if kind:
            filters.append({"term": {"kind": kind}})
        q = {
            "bool": {
                "should": [
                    {"multi_match": {"query": query, "fields": ["title_en^2", "keywords_en^1.5", "text_en"]}},
                    {"multi_match": {"query": query, "fields": ["title_ur^2", "text_ur"]}},
                ],
                "minimum_should_match": 1,
                "filter": filters,
            }
        }
        res = self.es.search(index=self.index, query=q, size=k)
        return [
            Hit(KDoc(**{f: h["_source"][f] for f in KDoc.model_fields}), float(h["_score"]), r)
            for r, h in enumerate(res["hits"]["hits"], 1)
        ]


# ------------------------------------------------------------------------------------------------ dense + hybrid + fallback
class DenseSearch:
    name = "dense"

    def __init__(
        self,
        docs: list[KDoc],
        model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        embedder: Any = None,
    ):
        import numpy as np

        self.np, self.docs = np, docs
        if embedder is None:
            from fastembed import TextEmbedding

            fe = TextEmbedding(model_name=model)
            embedder = lambda texts: list(fe.embed([fold_urdu(t) for t in texts]))  # noqa: E731
        self.embed = embedder
        self.mat = self._norm(np.array(self.embed([f"{d.title}. {d.text}" for d in docs]), dtype=float))

    def _norm(self, a: Any) -> Any:
        return a / (self.np.linalg.norm(a, axis=1, keepdims=True) + 1e-12)

    def healthy(self) -> bool:
        return True

    def search(self, query: str, k: int = 5, lang: str | None = None, kind: str | None = None) -> list[Hit]:
        q = self._norm(self.np.array(self.embed([query]), dtype=float))[0]
        sims = self.mat @ q
        order = [
            int(i)
            for i in self.np.argsort(-sims)
            if (not lang or self.docs[i].lang == lang) and (not kind or self.docs[i].kind == kind)
        ]
        return [Hit(self.docs[i], float(sims[i]), r) for r, i in enumerate(order[:k], 1)]


class HybridSearch:
    """Reciprocal-rank fusion (RRF) of two backends: robust, parameter-light, and needs no score calibration."""

    def __init__(self, a: SearchBackend, b: SearchBackend, k_rrf: int = 60, depth: int = 20):
        self.a, self.b, self.k_rrf, self.depth = a, b, k_rrf, depth
        self.name = f"hybrid({a.name}+{b.name})"

    def healthy(self) -> bool:
        return self.a.healthy() and self.b.healthy()

    def search(self, query: str, k: int = 5, lang: str | None = None, kind: str | None = None) -> list[Hit]:
        fused: dict[str, float] = {}
        docs: dict[str, KDoc] = {}
        for backend in (self.a, self.b):
            for h in backend.search(query, self.depth, lang, kind):
                fused[h.doc.doc_id] = fused.get(h.doc.doc_id, 0.0) + 1.0 / (self.k_rrf + h.rank)
                docs[h.doc.doc_id] = h.doc
        ranked = sorted(fused.items(), key=lambda x: -x[1])[:k]
        return [Hit(docs[i], s, r) for r, (i, s) in enumerate(ranked, 1)]


class ResilientSearch:
    """Use the primary backend; if it raises, answer from the fallback and say so. Search never takes the assistant down."""

    def __init__(self, primary: SearchBackend, fallback: SearchBackend):
        self.primary, self.fallback = primary, fallback
        self.name = primary.name
        self.fallbacks = 0

    def healthy(self) -> bool:
        return self.primary.healthy()

    def search(self, query: str, k: int = 5, lang: str | None = None, kind: str | None = None) -> list[Hit]:
        try:
            return self.primary.search(query, k, lang, kind)
        except Exception:  # noqa: BLE001
            self.fallbacks += 1
            log.warning(
                "search backend %s failed; answering from %s",
                self.primary.name,
                self.fallback.name,
                exc_info=self.fallbacks == 1,
            )
            return self.fallback.search(query, k, lang, kind)


def make_search(settings: Settings, docs: list[KDoc]) -> SearchBackend:
    """Elasticsearch (optionally hybrid with dense vectors) when configured and reachable, else our own BM25."""
    bm25 = Bm25Search(docs)
    if not settings.elasticsearch_url:
        return bm25
    try:
        es = ElasticSearchBackend(
            settings.elasticsearch_url,
            settings.es_urdu_analyzer,
            settings.es_index_prefix,
            settings.elasticsearch_api_key,
        )
        if not es.healthy():
            raise ConnectionError("Elasticsearch did not answer")
        es.index_docs(docs)  # idempotent: same ids, same content
        primary: SearchBackend = es
        if settings.dense_enabled:
            primary = HybridSearch(es, DenseSearch(docs, settings.embedding_model))
        log.info("search backend: %s", primary.name)
        return ResilientSearch(primary, bm25)
    except Exception:  # noqa: BLE001
        log.warning("Elasticsearch is configured but unavailable; using the built-in BM25", exc_info=True)
        return bm25
