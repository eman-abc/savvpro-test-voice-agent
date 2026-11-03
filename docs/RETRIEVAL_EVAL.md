# Retrieval evaluation

How well does each way of searching the clinic's knowledge find the right fact, in English, in Urdu, and in Urdu typed on the "wrong"
keyboard? This page says what was measured, what the numbers show, and, just as important, what they do **not** show.

## What was measured
- **Corpus:** 27 facts (10 FAQ, 8 policies, 6 services, 3 providers) in English and Urdu = 54 documents, generated from `clinic.yaml`.
- **Queries:** 79 labelled queries (`backend/eval/queries.yaml`) in four tiers: `direct` (shares words with the answer, 14),
  `paraphrase` (little overlap, 23), `urdu` (Urdu script, 30) and `roman` (Roman Urdu, 12). Each names the fact(s) that answer it.
- **Robustness probe:** the 30 Urdu queries again, retyped with Arabic-keyboard letters (ي ك ه instead of ی ک ہ), called `urdu_variant`.
  It is reported separately and is not part of the overall score.
- **Metrics:** Recall@k (share of the labelled facts found in the top k) and MRR (reciprocal rank of the first right fact), computed per
  fact, so the English and Urdu versions of a fact count once. Overall figures carry a 95% bootstrap interval.
- **Methods:** my own BM25; Elasticsearch 9.5.1 with the Urdu field analysed four ways (`standard`, built-in `arabic`, built-in
  `persian`, and a custom `urdu_custom`); dense multilingual embeddings (`paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions);
  and reciprocal-rank fusion of BM25/Elasticsearch with the dense results. Elasticsearch has no built-in Urdu analyzer, which is why
  four variants are compared. `urdu_custom` folds Arabic-script letter variants into Urdu forms, strips vowel marks, maps Arabic-Indic
  digits to ASCII and removes Urdu stop words.

## Results
79 queries over 27 facts (54 documents), run on one CPU core in a sandbox.

| Method | direct R@3 | paraphrase R@3 | urdu R@3 | **urdu typed on Arabic keyboard** R@3 | roman R@3 | R@1 | **R@3** (95% CI) | **MRR** (95% CI) | ms/query |
|---|---|---|---|---|---|---|---|---|---|
| `bm25-own` | 0.93 | 0.72 | 0.67 | 0.67 | 0.75 | 0.63 | 0.74 (0.64–0.83) | 0.75 (0.65–0.84) | 0.03 |
| `es-standard` | 0.96 | 0.76 | 0.70 | **0.40** | 0.79 | 0.55 | 0.78 (0.69–0.86) | 0.73 (0.65–0.81) | 5.0 |
| `es-arabic` | 0.96 | 0.76 | 0.70 | **0.40** | 0.79 | 0.56 | 0.78 (0.69–0.86) | 0.73 (0.65–0.81) | 5.0 |
| `es-persian` | 0.96 | 0.76 | 0.72 | 0.72 | 0.79 | 0.58 | 0.79 (0.70–0.87) | 0.74 (0.66–0.82) | 4.9 |
| `es-urdu_custom` | 0.96 | 0.76 | 0.72 | 0.72 | 0.79 | 0.56 | 0.79 (0.70–0.87) | 0.73 (0.64–0.81) | 4.2 |
| `dense` | 0.93 | 0.54 | 0.63 | 0.63 | 0.62 | 0.51 | 0.66 (0.56–0.75) | 0.69 (0.60–0.77) | 12.2 |
| `hybrid(bm25-own+dense)` | 0.93 | 0.78 | 0.73 | 0.73 | 0.83 | 0.62 | 0.80 (0.71–0.88) | 0.79 (0.71–0.86) | 12.7 |
| `hybrid(es-urdu_custom+dense)` | 0.93 | **0.85** | 0.77 | 0.77 | 0.79 | 0.60 | **0.82** (0.75–0.89) | 0.79 (0.72–0.86) | 21.8 |

## What the numbers show
1. **One result is clear: Urdu typed with Arabic-keyboard letters breaks the `standard` and `arabic` analyzers** (0.70 falls to 0.40)
   but not `persian`, `urdu_custom` or my own BM25. This is also an automated regression test against real Elasticsearch.
   The cheapest fix is the built-in `persian` analyzer; the custom analyzer did not beat it on this set.
2. **Hybrid fusion helps recall on hard phrasings:** on `paraphrase` queries Elasticsearch alone scores 0.76 and the hybrid 0.85, and the
   dense model alone is weakest there (0.54). It is suggestive rather than conclusive: the overall intervals overlap
   (hybrid 0.75–0.89 against Elasticsearch alone 0.70–0.87).
3. **Fusion does not help precision at rank 1:** my own BM25 has the best R@1 (0.63) against 0.60 for the best hybrid. The documents are
   short and titled, so exact words are a strong signal.
4. **On clean queries the four Elasticsearch variants are indistinguishable.** Their overall R@3 values (0.78–0.79) sit well inside
   each other's intervals. Do not read anything into 0.78 against 0.79.

## A flaw this evaluation found, and the fix
The dense model fell from 0.63 to **0.32** on keyboard-variant queries, and the hybrids inherited the weakness (0.65). The embedding model
tokenises different Unicode letter variants differently. The fix is to fold Urdu letter variants and strip vowel marks *before* embedding
(`fold_urdu`); after it the variant score equals the clean score (0.63, and the best hybrid 0.77).

## What the numbers do NOT show
- **The queries were written by the person who wrote the documents**, so every score is optimistic. These are a pipeline demonstration,
  not a benchmark. Real, held-out caller queries, reviewed by a native Urdu speaker, would be the next step.
- **Roman Urdu is flattered:** many of those queries contain English words ("parking", "insurance", "phone number"), which match the
  English documents. It is not evidence of Roman Urdu support.
- **27 facts is tiny.** At this size my own BM25 (0.03 ms, no dependencies) is a perfectly good choice. Elasticsearch earns its place
  with scale, per-field analyzers, aggregations (the call-insights tab uses them) and operations tooling, not on this corpus.
- **No paired significance test.** The intervals are per method; a paired bootstrap between two methods is the proper next step.
- One embedding model, fusion constant fixed at 60, one run of the sandbox's single core for latency.

## Reproduce
```bash
docker compose --profile knowledge up -d elasticsearch          # or any Elasticsearch 9.x
pip install -r backend/requirements-dense.txt                    # adds the optional embedding model
cd backend && python -m eval.run_eval --es http://localhost:9200 --out ../docs/RETRIEVAL_EVAL_RESULTS.md
pytest -m integration                                            # includes the keyboard-variant regression test
```
