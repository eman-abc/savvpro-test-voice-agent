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

## Reliability

- **Idempotent turns.** The client sends how many turns it has seen. A retry of the turn the server already
  handled gets the stored reply back (including the booking confirmation) instead of being processed again;
  an out-of-sync client gets a 409. Verified in a real browser by dropping the response to "yes".
- **Start-up is serialised** across workers and replicas with a Postgres advisory lock (migrations and
  seeding run once); verified with 4 workers starting at once on Postgres (no duplicate rows).
- **Migrations** are Alembic; a test builds the schema from them and fails if it differs from the models.
- **Observability**: `/metrics` (Prometheus), JSON logs with request ids, optional Sentry.

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
- **A real SMS/WhatsApp sender** (the interface and the webhook exist; the adapter needs a provider account).
- **Native-speaker review of the Urdu**, Urdu text-to-speech voices on every device, and a wider Urdu lexicon.
