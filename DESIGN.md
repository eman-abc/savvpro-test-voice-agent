# Design

## Use case

**A family clinic's front-desk phone line**: book, reschedule and cancel appointments, check upcoming
visits, and answer common questions (hours, parking, what to bring, policy). It was chosen because it is a
real job with hard rules (hours, provider schedules, notice windows, identity checks) that a rules-based
brain can handle honestly, and because callers are messy in predictable ways.

## Architecture

```
 Browser                                   FastAPI (one container)                    SQL (SQLite/Postgres)
 ───────                                   ───────────────────────                    ─────────────────────
 mic -> Web Speech API -> text ──POST /api/calls/{id}/turn──> Brain.handle(state, text)
                                                │               ├─ normalise (numbers, clock phrases)
                                                │               ├─ safety rules (emergency)
                                                │               ├─ intent scoring + FAQ retrieval
                                                │               ├─ global commands (human, repeat, start over)
                                                │               └─ step handler (slot filling)
                                                │                        │ calls
                                                │                        v
                                                │               ClinicService (availability, book,
                                                │                reschedule, cancel; enforces rules)
 TTS <- reply + trace <───────────────────────  ┘                        │
 React shows transcript + decision ledger                                 v
                                                                 appointments, appointment_slots,
                                                                 call_sessions, call_turns, audit_log
```

Dialogue state is a small JSON document stored on the call row after every turn, so the server is
stateless between requests and can run several workers.

## How the brain decides (in order, every turn)

1. **Normalise**: lower-case, strip accents, convert number words ("three thirty pm" -> "3:30 pm"), expand
   "half past two", "quarter to three".
2. **Emergency rule**: keywords such as "chest pain" end the call with 911 advice. Nothing overrides it.
3. **Intent scoring**: each intent has weighted regex patterns. Score = strongest pattern + 0.3 x the rest.
   Confidence = `top / (top + second + 1)`. Three outcomes:
   - confidence >= 0.55: act;
   - two intents close together: **ask** ("reschedule or cancel?");
   - weak: treat as not understood.
   "Specific beats generic" rules stop "cancel my appointment" also scoring as "book".
4. **FAQ retrieval**: BM25 over question + keywords (not answers) returns a **pre-written** answer. Nothing
   is generated. Action words are stop-words so "I want to cancel" is an action, not a policy question.
5. **Global commands**: human/frustration -> hand-off with a context summary; repeat; start over; goodbye.
6. **Step handler** for the current step (`ROUTE`, `IDENTIFY`/`REGISTER`, `SELECT`, `COLLECT`, `OFFER`,
   `CONFIRM`, `MORE`). Inside `COLLECT` the brain fills a frame of slots (service, doctor, date, time) in
   whatever order the caller gives them, validates each against the rules, and asks only for what is missing.

### Messy-caller behaviours

| Situation | Behaviour |
|---|---|
| "Thursday at 3, no wait, Friday morning" | Negated mentions are skipped; the last mention wins |
| "actually make it 3 pm" at the confirm step | Slot corrected, availability re-checked, asks to confirm again |
| "yes, but with Dr. Nguyen" | A change inside a "yes" is treated as a correction, not a confirmation |
| "I can't make it to my appointment" | Cancel and reschedule tie, so it asks |
| Exact time is taken | Offers the three closest open times that day |
| Day is full | Searches forward and offers the next openings |
| Someone books the slot while the caller decides | Write-time check and unique index refuse it; apologises and re-offers |
| Question mid-task ("what do I need to bring?") | Answers, then repeats the pending question |
| Weekday answer to a date question ("Saturday") | Treated as a date, not as the hours FAQ |
| Birth date said with the request | Stripped before slot extraction so it isn't read as the appointment date |
| Wrong name or date of birth | Never says which part failed; hint to register after 2 failures; hand-off after 3 |
| Silence or gibberish | Re-prompts with an example; hand-off after 3 misses |
| Visit inside the notice window | Warns about the late-cancellation fee and asks first |

## Rules document

`backend/config/clinic.yaml` is the only source of business rules; it is validated by Pydantic at start-up
(unknown service on a provider, backwards time window, service nobody offers... all fail fast). Policy
values (24-hour notice, 30/15-minute early arrival for new/established patients, 15-minute late grace)
follow published outpatient policies, notably Brigham and Women's Urology and Rush University Medical
Group (see the comment at the top of the file). Rules enforced from it:

- opening hours per provider, lunch gaps, holidays, minimum lead time, maximum days ahead;
- providers only offer certain services; some services need a new patient (new patient visit) or an
  established one (follow-up); first-time patients booking a general visit are moved to the 40-minute new
  patient visit;
- the notice window decides what counts as a late change.

## Data

| Table | Purpose |
|---|---|
| `patients`, `providers` | fake people; providers are synced from the YAML |
| `appointments` | one row per visit (status `booked` or `cancelled`) |
| `appointment_slots` | one row per 15-minute step an appointment occupies; **`UNIQUE(provider_id, slot_start)`** |
| `call_sessions`, `call_turns` | dialogue state, transcript, per-turn trace, outcome, hand-off summary |
| `audit_log` | who booked, moved or cancelled what (`voice:<call>` or `staff:<user>`) |
| `staff_users` | bcrypt-hashed passwords |

**No double-booking** is enforced by the database, not just by code: two callers racing for overlapping
times both try to insert slot rows and the unique index lets one through. Reschedule frees the old slots
and takes the new ones in one transaction, so a failure leaves the original untouched. This was tested
with real threads on SQLite and on Postgres (8 threads, one winner).

"Established patient" means at least one past non-cancelled visit, so the seed data gives established
patients a past visit.

## Security and privacy

- Callers are verified by conversation (name + date of birth), not a login; the replies reveal nothing about
  which detail was wrong.
- Staff: bcrypt, JWT in an HttpOnly SameSite cookie, CSRF double-submit header on every state-changing
  call, lockout after 5 failures, constant-time-ish unknown-user path.
- Strict CSP (self only, fonts are bundled), `X-Frame-Options: DENY`, no-store on API responses.
- Per-address rate limiting; call length, idle timeout and utterance length are capped. `X-Forwarded-For`
  is believed only as far as `TRUSTED_PROXY_COUNT` (the real client is that many entries from the right),
  so a client cannot dodge the limiter by inventing the header.
- **Identity lockout that survives redialling**: failed checks are counted per *phonetic* name in the
  database (`verify_attempts`), not per call, so hanging up and calling back doesn't reset the guess
  counter and spelling variants share one counter. A locked name is refused even with the right date of
  birth until the lock expires. The reply reads back what was heard, never which part was wrong.
- **Revocable staff sessions**: every sign-in is a row in `staff_sessions` (JWT `jti`). Logout, expiry, or a
  password change really end the session, so a copied cookie stops working.
- Logs are JSON and contain ids, intents and timings only, never what the caller said.
- Phone numbers are shown only as last four digits to staff; fake data only; old calls are purged.
- Prod refuses to start with the default JWT secret.

## Channels and the perception / decision boundary

The brain is channel-agnostic: `Brain.handle(state, text, asr_confidence)` is the only entry point, and `app/turns.py:run_turn` is the
single place a turn is loaded, decided, saved and measured. The web page, WhatsApp text and WhatsApp voice notes all call it, so
behaviour (rules, language, corrections, policies) is identical everywhere. **AI lives at the edges as perception**: a pretrained
speech model produces `{text, confidence}`; code decides what to do with it. A low confidence makes the rules read back what was heard
(`HEARD` step) for free-form answers only, never for yes/no steps, and never for emergency words, which are acted on immediately.
Provider choice is behind small interfaces (`SpeechToText`, `Notifier`), with fakes for tests and the browser recogniser as fallback.

**WhatsApp.** A signed webhook (HMAC-SHA256 of the raw body with the app secret, constant-time compare; no secret configured means the
endpoint does not exist) answers 200 immediately and processes in the background, because Meta retries slow webhooks. Idempotency is
**insert-first on a primary key**: a check-then-insert let a retry that raced the original through on Postgres (found by running the real
stack, not by the unit tests). Each phone number maps to a chat session (`channel='whatsapp'`); a bare "1" answers a pending reminder or
offer unless the patient is mid-conversation. Outbound messages respect the 24-hour rule: free-form text only after the patient wrote
(tracked per number), an approved template otherwise, or a recorded `window_closed` status. Delivery receipts update a separate
`delivery` field so conversation outcome and transport status never overwrite each other. Reminders retry failed or window-closed sends
with back-off, three times at most.

## Languages

One set of rules, two languages. What the caller says is first **normalised into a small canonical English vocabulary**
(`urdu.to_canonical`): Urdu script, Urdu digits, spoken numbers, clock idioms and Roman Urdu become "tomorrow 10 am", "cancel",
"my name is ..." and so on, then the unchanged intent, entity and state-machine code runs. That is why corrections, policies and
safety rules behave identically in both languages and why adding a language is a lexicon plus a message catalogue, not a new brain.
Replies are **templates keyed by their English source text**, translated at one choke point (`Brain._`); dates, times, names, lists and
FAQ answers are formatted per language. Tests enforce that every template has a translation, that placeholders match, and that no
English leaks into an Urdu reply. Names are transliterated and compared with a phonetic key; the caller's own Urdu spelling is
echoed back. Trade-offs: the lexicon is hand-written (coverage grows with use, the trace shows what failed), and code-switching
is handled by treating Latin words as they are.

## Waitlist and two-way messaging

`WaitlistService` listens to a hook on the domain service: whenever slots are freed (cancel or reschedule), each freed start goes to
the first waiting entry that matches service, provider, date, time window, real bookability and "patient is free then"
(first come, first served). An offer has an expiry and is passed on if unanswered or declined; accepting goes through the normal
booking path, so the database's no-double-booking constraint still decides races. Messages live in an **outbox table** with status
and the patient's reply, and are delivered by a swappable `Notifier`. Inbound replies (webhook, protected by a shared secret, or the
staff "reply as patient" control) go through the same interpreter as the voice brain. A late cancellation asks for a second
confirmation. A background task expires offers every minute.

## Silence

The page sends an empty turn after 20 seconds of nothing. The brain says "are you still there?" once (repeating the pending question),
ends the call politely after a second silence, and any real input resets the count. Silence is deliberately **not** counted as a
misunderstanding, so it never escalates to a human by itself.

## Delivery

CI gates every change; CD publishes an immutable, labelled image only after CI passes on `main`, asks the host to deploy it, and
waits for `/readyz`. Migrations run at start-up under a database lock, so replicas can start together. `/api/version` ties a running
instance to a commit.

## Knowledge layer: three stores, one source of truth

**Decision.** `clinic.yaml` stays the source of truth for clinic facts. One pipeline derives typed documents from it and loads three projections:
SQL (a ledger of what was loaded, for change detection), Elasticsearch (search and aggregations) and Neo4j (relationships and provenance). Bookings stay in SQL.

**Why bookings are not in the graph.** They need transactions and constraints: one row per 15 minutes per provider with a unique key is what prevents a double booking,
and a reschedule must succeed or fail atomically. A graph database is the wrong place for that. The graph answers a different question: which rules govern this
service, which FAQ explains them, who offers it, and where each fact came from (`source` on every node). It stores patient ids only, never names, birth dates or phone numbers.

**Adapter with fallback.** `SearchBackend` and `KnowledgeGraph` are small interfaces. Elasticsearch / Neo4j are the primary implementations; my own BM25 and an in-memory graph
are the fallbacks, wrapped so a runtime failure degrades instead of failing the call. The in-memory graph is also the test oracle: a parity test asserts that the Cypher and
the Python return identical answers for every fact. Labels and relationship types cannot be Cypher parameters, so the Cypher is interpolated only from a fixed allow-list.

**GraphRAG-style retrieval.** Search finds the facts; the graph expands each to what it is connected to; the answer carries its sources. After a FAQ the graph also
suggests the right service and who offers it.

**Urdu search.** Elasticsearch has no built-in Urdu analyzer. The Urdu field is analysed four ways and compared (`docs/RETRIEVAL_EVAL.md`). The measured finding is
narrow and real: Arabic-keyboard letter variants break `standard` and `arabic`, while `persian` and a custom analyzer hold. The evaluation also exposed a flaw in the dense
model (it fell apart on the same variants) which is fixed by folding Urdu letters before embedding.

**Ingestion.** Validate (typed documents, no dangling graph references, all problems reported together) then diff by content fingerprint then reconcile each projection
against SQL. Idempotent, self-healing (a wiped index is rebuilt) and deletions propagate. `--dry-run` writes nothing.

**Call transcripts.** Real calls come from SQL. Synthetic calls are generated by replaying the golden scenarios through the real brain, sometimes corrupted, so outcomes and
intents are genuine but the volume is invented. They are labelled `source: synthetic` and exist to exercise the aggregation ("what did callers say before a handoff?").

## The agent brain (optional, `BRAIN=agent`)

**Same interface, different engine.** `AgentBrain.start/handle` returns the same `TurnResult` as the rules `Brain`, so the web page, WhatsApp and the staff console are unchanged.
It is a LangGraph state graph: `guard` then `agent` which loops through `tools` and back, bounded by a step cap, with `limit` as the exit that hands over to staff.

**Principle: the model plans and phrases; code owns state, safety and every write.**
- *State lives in code.* Verification, the staged action and the history are in `DialogState`. The model sees them as authoritative facts in each turn's system prompt, not
  as something it has to remember. Only user and assistant text is kept between turns: stored tool messages could end up orphaned and make a provider reject the request.
- *Emergency guard before the model.* The same deterministic matcher as the rules engine runs first; the model never sees the message.
- *Identity gate.* Appointment tools refuse until `verify_patient` succeeded. Failures are generic. A "new patient" who matches an existing record is handed to staff
  rather than given that record.
- *Propose, then confirm.* `propose_*` writes nothing. `confirm_pending` executes only if the proposal is from an earlier turn **and** the caller's latest message is a clear
  yes, which is checked in code because a model can claim a confirmation that never happened. The tests make the "model" lie on purpose and check the code refuses.
- *Degradation.* Provider outage: the rules engine answers that turn. Runaway tool loop: capped, then staff.

**Failure modes not solved.** Prompt injection through the caller's words can still steer the conversation, but not past the gates (the tools can only do validated, owned,
confirmed things). Dates and numbers phrased in Urdu by the model are unverified. Cost, latency and non-determinism (temperature 0 does not guarantee repeatability) are not
measured. **No claim is made that the agent beats the rules engine**: the harness (`eval/agent_eval.py`) exists, and it has not been run against a live model.

**When to prefer which.** The rules engine is cheap, instant, exactly testable and predictable; it is brittle outside its patterns. The agent understands free-form and mixed-language
speech but is slower, costs money per turn and is harder to prove correct. In a medical setting the sensible shape is the one built here: the model for understanding and wording,
deterministic code for anything that changes a record or decides safety.

**A safety finding.** Writing an Urdu emergency test for the agent exposed that the *rules engine's* emergency matcher missed real phrasings: "pain in my chest", "he is not
breathing", "سینے میں شدید درد ہے", "میرا بچہ بے ہوش ہو گیا ہے". The matcher was an exact-phrase regex. It is now a shared proximity matcher (all words of a keyword within a few words,
any order), with more English keywords and an Urdu-script list, used by both brains. The cost is occasional false alarms, deliberately: a repeated call costs far less than a missed emergency.

## Reliability

- **Idempotent turns.** The client sends how many turns it has seen. A retry of the turn the server already
  handled gets the stored reply back (including the booking confirmation) instead of being processed again;
  an out-of-sync client gets a 409. Verified in a real browser by dropping the response to "yes".
- **Start-up is serialised** across workers and replicas with a Postgres advisory lock (migrations and
  seeding run once); verified with 4 workers starting at once on Postgres (no duplicate rows).
- **Migrations** are Alembic; a test builds the schema from them and fails if it differs from the models.
- **Observability**: `/metrics` (Prometheus), JSON logs with request ids, optional Sentry.

## Testing strategy: additions for the knowledge layer and the agent

- **Real services, not mocks, where it matters.** `pytest -m integration` runs against real Elasticsearch and Neo4j (skipped when not configured; CI uses service containers).
- **Parity tests** between implementations (Cypher vs in-memory graph; Elasticsearch aggregation vs a Python oracle; rules engine vs agent on emergencies).
- **A scripted chat model** drives the real graph, tools and database with no network, so the safety design is tested by making the "model" misbehave.
- **Mutation checks** (by hand): break the code, confirm the test fails. This caught three of my own tests that passed for the wrong reasons.
- **A measured claim has a regression test**: the keyboard-variant finding is asserted against real Elasticsearch, so it cannot silently stop being true.
## Testing strategy

| Layer | What it proves |
|---|---|
| Unit | parsers, intent scoring, FAQ retrieval, rules validation |
| Scripted dialogues | the messy-caller behaviours listed above |
| **Golden scenarios** (38 YAML calls) | every behaviour end to end; scored for task completion, turns per call and intent accuracy, and runnable live from the staff UI |
| **Property-based fuzzing** (Hypothesis) | arbitrary text and random multi-turn conversations never crash the brain, corrupt state, or leave half-booked slots |
| Service and race tests | no double-booking, atomic reschedule, real threads on SQLite and Postgres |
| API/security | CSRF, lockout, session revocation, header spoofing, idempotent retries, metrics access |
| Real browser (Playwright) | call flow, lost-response retry, staff area, scenarios tab, **axe accessibility** on every screen (it found two real issues) |

## Time

All datetimes are **naive and in the clinic's timezone**. This is simple and correct for a single
location; a multi-site product would store UTC plus a location timezone. The server clock decides "today",
so the demo reads the same wherever the viewer is.

## Trade-offs and what I'd do next

- Rule-based NLU is brittle outside its patterns (rare phrasing, accents in names, "for Wei Zhang" without
  an introduction). The trace shows exactly why a turn failed, which makes fixing it quick, and that
  transparency is the point of this design.
- English only. Web Speech API is Chrome and Edge only; swap in Deepgram/Whisper behind an interface.
- Real telephony (Twilio) is an adapter around `Brain.handle`; not built.
- In-memory rate limiter/lockout; use Redis for several replicas. `X-Forwarded-For` is trusted as given.
- Schema via `create_all`; add Alembic migrations before the first schema change in production.

### Not done (and why)

- **Real telephony and a real SMS provider.** They need paid accounts and consent/recording rules. Both are
  adapters behind existing interfaces (`Brain.handle`, `Notifier`).
- **UTC timestamps with per-location timezones.** Fine for one clinic; needs a schema change and DST tests
  for several.
- **MFA, per-person staff accounts and roles**; **multi-clinic** support; **manual booking and provider time-off screens**
  (time-off still lives in the YAML).
- **HIPAA-grade handling** (BAAs with every vendor, encryption at rest, transcript minimisation). This
  project uses fake data only and must not be pointed at real patients as is.
- **Generated TypeScript API types, OpenTelemetry tracing, per-PR preview deploys.**
- **Multi-worker performance numbers**: my sandbox had one CPU core, so scaling is untested.
- Several requests in one sentence ("cancel Tuesday and book Thursday"): the brain handles one request at a time.
- **Live verification of WhatsApp and Deepgram**: both are built and tested against fakes and a local stand-in for Meta's API, but not against the real services. Also not built: audio replies (text-to-speech) and WhatsApp template management.
- **Native-speaker review of the Urdu**, Urdu text-to-speech voices on every device, and a wider Urdu lexicon.
