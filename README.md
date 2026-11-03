# Clinic voice assistant

A voice assistant that does one real phone job end to end: **booking, rescheduling and cancelling clinic
appointments**, plus answering common questions. The caller speaks, the assistant listens, decides and
talks back, and a side panel shows every decision it makes.

> **The brain is plain code, not an AI model.** No GPT, Claude or Gemini decides anything at run time.
> Intent scoring, slot filling, scheduling rules and the state machine are all in `backend/app/brain` and
> `backend/config/clinic.yaml`. See [DESIGN.md](DESIGN.md).

```
Caller speaks -> speech to text -> THE BRAIN (our rules) -> text to speech -> caller hears the reply
(browser)        (browser)         FastAPI + SQL            (browser)
```

## What you get

- **Call page** (`/`): talk or type. Shows a progress stepper, "what I have so far", and a decision ledger
  per turn (intent and confidence, what it picked up, which rule fired, what it did, what it said).
- **Staff area** (`/staff`): sign in, week calendar by provider, appointment list with cancel, call log
  with full transcripts and traces, and a few headline numbers.
- **Scenarios tab**: replays 61 scripted calls on a throw-away database and scores task completion, turns
  per call and intent accuracy, live, without touching real data.
- **Urdu and English**: pick اردو on the start screen, or just speak Urdu (script, Roman Urdu or a mix) and the
  assistant answers in Urdu script. See "Urdu" below.
- **Waitlist and two-way reminders**: when nothing fits, callers can join a waitlist; a cancellation offers the slot to
  the first match; reminders say "reply 1 to confirm, 2 to cancel". Staff see **Messages** and **Waitlist** tabs.
- **WhatsApp channel**: chat with the same assistant over WhatsApp (English or Urdu), send **voice notes**, and receive reminders
  and waitlist offers there (Meta Cloud API; setup in [docs/WHATSAPP_SETUP.md](docs/WHATSAPP_SETUP.md)).
- **Speech at the edges**: a pretrained speech-to-text model (Deepgram, optional) turns audio into text and a confidence number;
  when it is unsure the assistant **reads back what it heard** before acting. The browser recogniser is the fallback.
- **"Are you still there?"** after a pause; the call ends politely after a second silence.
- Handles messy callers: mid-sentence corrections, changing the time at the confirm step, ambiguous
  requests, wrong identity (with a lockout that survives hanging up and redialling), silence, frustration,
  emergencies, mid-task questions, double-booking races, and a dropped connection mid-call (turns are
  idempotent, so a retried "yes" never books twice).
- Everything is **fake data** (names, 555-01xx numbers, address).

## Run it locally

Requirements: Python 3.11+, Node 20+.

```bash
# 1. backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests                             # 630+ tests (unit, scripted calls, Urdu, fuzzing, API, agent, knowledge)
uvicorn app.asgi:app --reload --port 8000

# 2. frontend (second terminal)
cd frontend
npm ci
npm run dev                              # http://localhost:5173 (proxies /api to :8000)
```

**Windows (PowerShell)**: the same steps, with these differences. Environment variables are set per session
(`$env:STT_PROVIDER = "fake"`), the virtual environment is activated with `.venv\Scripts\Activate.ps1`
(run `Set-ExecutionPolicy -Scope Process RemoteSigned` first if scripts are blocked), and the `make` shortcuts
are not available: use the underlying commands (`npm run build`, then copy `frontend/dist` to `backend/static`).

```powershell
cd backend
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest tests
uvicorn app.asgi:app --reload --port 8000
```

Voice input uses the browser's Web Speech API, which works in **Chrome and Edge** (needs a mic and an
internet connection). In other browsers, typing works the same way. Open the "Callers to try" panel for
fake patients to verify as.

**One-process build** (what production runs): `make build` copies the built UI into `backend/static`, then
`uvicorn app.asgi:app` serves UI and API from one port.

## Docker

```bash
docker compose up --build        # app + Postgres, then open http://localhost:8000
```

## Deploy (Render, free tier)

1. Push this repo to GitHub.
2. Render dashboard -> **New -> Blueprint** -> choose the repo. `render.yaml` builds the `Dockerfile` and
   generates a random `JWT_SECRET`.
3. When the deploy is live, share `https://<service-name>.onrender.com`.

Free instances sleep after about 15 minutes idle and have no persistent disk, so the first visit after a
pause takes a few seconds and data resets on redeploy. That is fine here because fake data is re-seeded
on every start. Note that HTTPS is required for the browser to allow the microphone, which Render provides.

**Before sharing it publicly:** with `DEMO_MODE=true` the sign-in page shows the demo staff password
(everything behind it is fake). For anything real set `DEMO_MODE=false` and a strong `STAFF_PASSWORD`.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `dev` | `prod` enforces a real `JWT_SECRET` |
| `DATABASE_URL` | SQLite file | Postgres: `postgresql://user:pass@host:5432/db` |
| `JWT_SECRET` | dev value | 24+ random characters in prod |
| `COOKIE_SECURE` | `false` | `true` behind HTTPS |
| `STAFF_USERNAME` / `STAFF_PASSWORD` | `reception` / demo value | the env var is the source of truth |
| `DEMO_MODE` | `true` | seeds fake data, shows sign-in hints, enables "Reset demo data" |
| `RATE_LIMIT_PER_MINUTE` | `120` | per client address, API only |
| `TRUSTED_PROXY_COUNT` | `0` | proxies in front that append to `X-Forwarded-For` (Render: `1`) |
| `USE_MIGRATIONS` | `false` | run Alembic at start-up (the Docker image sets `true`) |
| `METRICS_TOKEN` | unset | set to expose `GET /metrics` (Prometheus); never exposed unauthenticated in prod |
| `VERIFY_LOCK_FAILURES` / `VERIFY_LOCK_MINUTES` | `5` / `15` | failed identity checks per name before it is locked |
| `REMINDERS_ENABLED` / `REMINDER_HOURS` | `false` / `24` | background reminders (logs by default; plug in an SMS sender) |
| `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | unset | all four enable WhatsApp; until then its webhook does not exist |
| `WHATSAPP_ALLOWED_NUMBERS` | empty | comma-separated digits; others are ignored (recommended for the demo) |
| `WHATSAPP_TEMPLATE_NAME` | unset | approved template used when the 24-hour window is closed |
| `DEMO_WHATSAPP_NUMBER` / `DEMO_WHATSAPP_PATIENT` | unset / `Wei Zhang` | link a demo patient to your own number at start-up (never committed) |
| `STT_PROVIDER` | `none` | `deepgram` (needs `DEEPGRAM_API_KEY`), `fake` (tests/demos), or `none` (browser speech only) |
| `ASR_MIN_CONFIDENCE` | `0.55` | below this the assistant reads back what it heard |
| `ELASTICSEARCH_URL` / `ES_URDU_ANALYZER` | unset / `urdu_custom` | enables Elasticsearch search; analyzer is one of `standard`, `arabic`, `persian`, `urdu_custom` |
| `NEO4J_URI` / `NEO4J_PASSWORD` | unset | enables the Neo4j graph (AuraDB Free works: `neo4j+s://...`) |
| `DENSE_ENABLED` | `false` | multilingual embeddings + hybrid fusion (`pip install -r backend/requirements-dense.txt`) |
| `BRAIN` | `rules` | `rules` or `agent` |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` | `none` / unset / unset | for `BRAIN=agent`: `groq`, `openai` or `ollama` |
| `SENTRY_DSN` | unset | optional; also `pip install sentry-sdk` |
| `INBOUND_SECRET` | unset | shared secret for `POST /api/inbound/message` (patient replies); the endpoint is off until set |
| `WAITLIST_OFFER_MINUTES` | `30` | how long a waitlist offer stays open before it passes to the next person |
| `BACKGROUND_ENABLED` | `true` | every minute: expire waitlist offers (and send reminders if `REMINDERS_ENABLED`) |
| `CALL_IDLE_MINUTES` / `SESSION_RETENTION_HOURS` | `30` / `48` | idle timeout; old call logs are purged |

Copy `.env.example` to `backend/.env` for local overrides. No secrets are committed.

## Try these calls

| Say | What it shows |
|---|---|
| "I need a flu shot" -> *Priya Sharma, July 23rd 1990* -> "any provider" -> "tomorrow at 10 am" -> "actually make it 3 pm" -> "yes" | slot filling, correction at confirm, booking |
| "Thursday at 3, no wait, Friday morning" | last mention wins |
| "I can't make it to my appointment" | asks reschedule or cancel instead of guessing |
| *Liam O'Connor, January 30th 1968*, cancel | late-cancellation rule (visit inside 24h) |
| *Olivia Johnson-Smith*, reschedule | two appointments: "the lab one" |
| "I'm a new patient" | registration, then forced to a 40-minute new-patient visit |
| "What do I need to bring?" mid-booking | answers, then returns to the same step |
| "This is useless" / "let me talk to a person" | hand-off with a context summary |
| "I have chest pain" | emergency rule overrides everything |

## Project layout

```
backend/
  app/brain/        text.py entities.py intents.py retrieval.py dialog.py   <- the brain
  app/services/     clinic.py     availability, booking, reschedule, cancel (all rules, one place)
  app/api/          calls.py auth.py staff.py deps.py schemas.py
  app/              main.py models.py rules.py security.py seed.py config.py db.py
  config/clinic.yaml  hours, services, providers, policy, FAQ (validated at start-up)
  scenarios/        66 golden conversations (YAML), also run live from the staff "Scenarios" tab
  migrations/       Alembic (initial schema); e2e/ browser tests; loadtest/ load script
  tests/            630+ tests: unit, dialogues, golden scenarios, fuzzing, service rules + race, API/auth
frontend/src/       pages/ components/ lib/   (React + TypeScript + Vite)
Dockerfile  docker-compose.yml  render.yaml  .github/workflows/ci.yml
DESIGN.md  AI_USAGE.md
```

## Tests and checks

```bash
cd backend && pytest tests --cov=app     # 630+ tests, about 90% coverage (CI requires 85%)
cd backend && pytest e2e                 # 18 real-browser tests incl. accessibility, run at a pinned clock (needs: make build; playwright install chromium)
cd backend && FUZZ_EXAMPLES=2500 pytest tests/test_fuzz.py   # heavier property-based fuzzing
cd backend && ruff check app tests e2e && ruff format --check app tests e2e
cd frontend && npm run typecheck && npm test && npm run build
make audit                               # pip-audit + npm audit
make loadtest                            # needs the server running on :8000
```

What the suites cover: unit tests for parsing and intents; **66 golden conversations** (16 in Urdu) (`backend/scenarios`);
property-based fuzzing (random utterances and conversations must never crash or corrupt state); the
double-booking race on real threads; security behaviour (lockout, session revocation, spoofed headers,
CSRF, idempotent retries); migrations match the models; reminders; metrics; and in a real browser: the
call flow, a **lost-response retry**, the staff area, the scenarios tab and **axe accessibility checks**
on every screen.

CI (`.github/workflows/ci.yml`) runs all of that, plus dependency audits, secret scanning (gitleaks), a
Docker build with a Trivy image scan and a container health check. CodeQL and Dependabot are configured.

## Operations

- **Health**: `/healthz` (process) and `/readyz` (database).
- **Metrics**: `GET /metrics` in Prometheus format: request counts and latency by route, turns by intent
  and decision, brain latency, misses, and call outcomes. Enabled with `METRICS_TOKEN`.
- **Migrations**: `alembic upgrade head` (or `USE_MIGRATIONS=true` to run at start-up). Start-up work is
  serialised across workers with a Postgres advisory lock. A test fails if models and migrations diverge.
- **Logs**: JSON, no utterances or personal data.
- **Reminders**: `REMINDERS_ENABLED=true` runs a background pass every minute; each visit is reminded once.
  The sender is an interface (`app/reminders.py`); the default only logs a masked number.

### Measured performance

On a **1-CPU sandbox** with SQLite and one worker (`loadtest/loadtest.py`): one caller gets p95 of 16 ms
per turn; 20 concurrent callers saturate at roughly 75 turns per second with p95 of 0.6 s (SQLite
serialises writes and there is one core). The brain itself does 0 to 8 queries and a few milliseconds of
work per turn. A 4-worker Postgres run on that same single core was *slower* because everything shared one
core, so that number says nothing about real scaling. Re-run `make loadtest` on your target hardware.

## WhatsApp, voice notes and speech

**Where AI is used, and where it isn't.** Pretrained speech models sit at the *edges*: they turn audio into text and report how
confident they are. Everything the assistant decides or says comes from the rules in `backend/app/brain`. The model never chooses an
action; a low confidence number only makes the rules ask "I heard X, is that right?" first.

- Browser microphone: records a short clip, `POST /api/stt` transcribes it (Deepgram), and the text goes through the normal turn
  together with the confidence. If the server provider is missing or fails, the page falls back to the browser's own recogniser.
- WhatsApp voice notes: downloaded from Meta, transcribed the same way (the patient's language is tried first, then the other),
  and answered with the transcript so the patient can see what was understood.
- Messages are deduplicated atomically (Meta retries webhooks), signatures are verified on every call, and the demo can be limited
  to your own number. Not built: spoken (audio) replies, which need a text-to-speech provider.

## Urdu (اردو)

- **Replies** are in Urdu script. Every reply the assistant can give is a template with an Urdu translation
  (`backend/app/brain/messages_ur.py`); a test fails if one is missing, and another fails if English leaks into an
  Urdu reply. Names, dates, times, the FAQ and the emergency number (`1122`, configurable) are localised from the rules file.
- **Understanding**: Urdu script, Urdu digits, spoken numbers ("انیس سو تراسی" = 1983), clock idioms
  ("ساڑھے تین بجے" = 3:30), Roman Urdu and English mixed in. Everything is mapped to the same canonical vocabulary the
  English rules use, so intents, corrections and policies behave identically in both languages.
- **Names** are transliterated and compared phonetically with the Latin-script records ("احمد خان" finds "Ahmed Khan").
- **Speech**: the browser uses `ur-PK` for recognition. Urdu *voices* depend on the device; if there is none the reply is
  shown as text and the page says so.
- **Please have a native speaker review the wording** before real use. I wrote it, and I can't hear it.

## Waitlist and two-way reminders

1. A caller who can't get a slot (or says "put me on the waitlist") is offered a place in line for that day/time window.
2. When an appointment is cancelled (by phone, by staff, or by a patient replying "2"), each freed slot goes to the first
   person in line whose service, provider, date and time fit, and who is free then. They get a message.
3. They reply **1** to book or **2** to skip (or yes/no, in English, Urdu or Roman Urdu). An unanswered offer expires
   after `WAITLIST_OFFER_MINUTES` and passes to the next person; if the slot was taken meanwhile they are told so.
4. Reminders go out before a visit with "reply 1 to confirm, 2 to cancel". Cancelling inside the notice window asks a
   second time because of the fee.

Messages are written to a database outbox and handed to a sender behind a small interface (`Notifier`). The default only
logs; an SMS or WhatsApp adapter implements `send`, and replies arrive on `POST /api/inbound/message` with the
`X-Webhook-Token` header. In the staff **Messages** tab you can answer *as the patient* to try the whole loop with no provider.
Replies are matched to patients by the last 7 digits of the phone number: use full E.164 matching in production.

## Continuous delivery

- **CI** (`ci.yml`) on every push: lint, backend tests (85% coverage gate), frontend, real-browser tests, dependency and
  secret scans, Docker build with a Trivy scan and a container health check. **CodeQL** and **Dependabot** are configured.
- **CD** (`deploy.yml`) runs only after CI succeeds on `main`: builds the image with build metadata, pushes it to GHCR
  (with provenance and an SBOM), triggers the host's deploy hook, then polls `/readyz` and prints `/api/version`.
  Set the repository secret `RENDER_DEPLOY_HOOK_URL` and variable `PUBLIC_URL` (an environment named `production` lets you
  require approval). Tag `vX.Y.Z` to run `release.yml` (versioned image + release notes).
- If you use the deploy hook, switch off the host's automatic deploys so a deploy happens only after CI passes.
- `/api/version` reports the version and commit, `/readyz` checks the database, `/metrics` is Prometheus (token-protected).

## Knowledge layer: Elasticsearch, Neo4j, ingestion and evaluation

> **Scope of this branch.** The original take-home required a rules-only brain. This branch deliberately goes further (a knowledge
> layer and an optional LLM agent). The rules engine is still the default (`BRAIN=rules`) and the safety rules are shared.

The clinic's facts live in `clinic.yaml`. One pipeline turns them into typed documents and loads three projections of the same data:

| Store | What it holds | Why this store |
|---|---|---|
| **SQL** | bookings, patients, and what the pipeline last loaded | transactions: no double booking, atomic reschedule |
| **Elasticsearch** | FAQ / policy / service / provider text (English + Urdu analyzers) and call transcripts | full-text search, per-language analysis, aggregations ("what did callers say before a handoff?") |
| **Neo4j** | providers, services, policies and FAQs as a graph; bookings as an id-only read model | relationships and provenance ("which rules govern this service, and where do they come from?") |

SQL stays the source of truth for bookings. The graph is a read-optimised knowledge layer, and it never stores names, birth dates or phone numbers.
Every layer is optional and falls back: no Elasticsearch means my own BM25, no Neo4j means an in-memory graph with identical answers (a parity test checks this),
so the deployed demo works with nothing else running.

```bash
docker compose --profile knowledge up -d elasticsearch neo4j     # local development only (security is off in this compose file)
export ELASTICSEARCH_URL=http://localhost:9200 NEO4J_URI=bolt://localhost:7687 NEO4J_PASSWORD=localdevpassword
cd backend && python -m app.knowledge.ingest --calls 300        # validate, load SQL + search + graph + 300 synthetic call transcripts
python -m app.knowledge.ingest --dry-run                         # report what would change; write nothing
```
The pipeline is **validated** (typed documents; a dangling graph reference stops it before anything is written), **idempotent** (run it twice and the second
run changes nothing) and **self-healing** (wipe the search index and the next run rebuilds it from SQL). The staff console has a **Knowledge** tab: search with
sources and graph facts, call insights, and a reload button.

**Retrieval evaluation:** [docs/RETRIEVAL_EVAL.md](docs/RETRIEVAL_EVAL.md): 79 labelled English / Urdu / Roman Urdu queries, BM25 vs Elasticsearch (four Urdu analyzers) vs
dense vs hybrid, with confidence intervals, a keyboard-variant robustness test, and an honest list of what it does not prove.

## Agent brain (optional)

`BRAIN=agent` swaps the rules engine for a **LangGraph** agent (`backend/app/agent`). An LLM plans and phrases; deterministic code owns everything that matters:

- an **emergency guard** runs before the model sees the message, with the same matcher as the rules engine (a parity test checks both agree);
- **identity gate**: appointment tools refuse until the caller is verified, and the model never learns which detail failed;
- **propose, then confirm**: `propose_*` tools write nothing; `confirm_pending` works only if the proposal is from an *earlier* turn and the caller's latest
  message is a clear yes, checked in code, not trusted from the model;
- a **step cap** hands runaway loops to staff; if the model is down, **the rules engine answers that turn**;
- every tool call is in the decision trace shown in the staff console.

```bash
BRAIN=agent LLM_PROVIDER=groq LLM_MODEL=<model-name> LLM_API_KEY=...   # also: openai, ollama (no key); one OpenAI-compatible client covers all three
python -m eval.agent_eval --brain both --provider groq --model <model-name> --limit 20     # outcome-based comparison on the golden scenarios
```
The agent is tested end to end with a scripted model (including attempts to bypass every gate), but **it has not been evaluated against a live LLM**: no
agent-vs-rules numbers are published until `eval.agent_eval` has been run with a real key.

## Known limits

English and Urdu only; Web Speech API needs Chrome or Edge; no real telephony (the brain is channel-agnostic, so a
Twilio adapter would be the next step); rate limiting and the staff-login lockout are in-memory (use Redis
with more than one replica); datetimes are naive clinic-local time (no DST handling beyond the clinic's
own zone); staff accounts are a single admin user (no MFA or roles). The full list of what is and isn't
done is in [DESIGN.md](DESIGN.md).
