# Changelog

## 1.4.0
- **Knowledge layer:** typed bilingual documents built from the YAML; search over my own BM25, Elasticsearch (four Urdu analyzer variants), dense embeddings and
  hybrid fusion, with automatic fallback; a Neo4j knowledge graph with an in-memory twin (parity tested); GraphRAG-style answers with sources.
- **Ingestion pipeline:** validated, idempotent, self-healing, deletions propagate; `--dry-run`; synthetic call transcripts; staff **Knowledge** tab.
- **Retrieval evaluation:** 79 labelled queries, bootstrap intervals, a keyboard-variant robustness test (`docs/RETRIEVAL_EVAL.md`).
- **LangGraph agent brain** (`BRAIN=agent`): emergency guard, identity gate, propose-then-confirm, step cap, rules-engine fallback; agent-vs-rules harness (not yet run live).
- **Safety fix:** the emergency matcher missed real phrasings in English and Urdu; now a shared proximity matcher used by both brains.
- Tests: 630+ (real-service integration tests behind `-m integration`), 66 golden scenarios, 18 browser tests. CI gains an integration job.

## 1.3.0
- **WhatsApp channel** (Meta Cloud API): signed webhook, chat over the same brain in English and Urdu, voice notes, reminders and waitlist
  offers with the 24-hour rule, delivery receipts, optional allowed-number list, demo number linking, staff channel panel.
- **Speech at the edges**: Deepgram speech-to-text behind an interface (browser fallback), microphone recording on the page, and
  **read-back of low-confidence speech** before the rules act on it.
- Windows: fixed a crash building the demo-patient hints (`%-d` is a Linux-only date code); a test now scans for such codes.
- Reminders back off and give up after three attempts. Fixed: reminders now use the configured sender; atomic webhook de-duplication
  (a retry racing the original); migration 0003.

## 1.2.0
- **Urdu**: replies in Urdu script; understands Urdu script, Roman Urdu and mixed speech; Urdu names, dates, times and FAQ;
  right-to-left UI with an Urdu font, `ur-PK` speech, Urdu quick replies.
- **Waitlist** with first-come-first-served offers that expire; **two-way reminders** ("reply 1 to confirm, 2 to cancel")
  with a late-cancellation confirmation step; inbound webhook (shared-secret protected); staff Messages and Waitlist tabs.
- **"Are you still there?"** prompt after a pause; the call ends politely after a second silence.
- Fixed: "No, 3 pm" was read as *not* 3 pm; names starting with a vowel sound broke phonetic matching;
  overlapping appointments hid each other in the calendar; Messages tab showed a result before refreshing.
- CD: image published to GHCR after CI passes, deploy hook and health check, release workflow, `/api/version`.

## 1.1.0
- Hardening: per-name identity lockout, revocable staff sessions, trusted-proxy handling, idempotent turns,
  metrics, Alembic migrations, golden scenarios, fuzzing, browser and accessibility tests, CI security scans.

## 1.0.0
- Rules-based clinic voice assistant: book, reschedule, cancel; FAQ; staff calendar and call log.
