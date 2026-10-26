# AI usage

## Tools

- **Claude (Sonnet 5.5) in the claude.ai chat interface**, used for planning, research, and to write and
  debug most of the code in this repository. It ran code in a sandbox (pytest, ruff, uvicorn, Playwright,
  a local Postgres) so its claims were checked by running things, not asserted.
- Claude's web search was used to find real clinic policies for the rules (see `clinic.yaml`) and to check
  Render's free-tier behaviour.
- **No AI model runs inside the product.** The brain is deterministic code, as the task requires.

## How it helped

- Compared use cases and picked clinic appointments; designed the slot-filling state machine and the
  intent confidence scheme.
- Wrote the backend, the 379 backend tests, the 15 browser tests, the React UI, the Dockerfile, CI and docs.
- Ran scripted "messy caller" conversations against the brain and used the output to find bugs.
- Drove a headless browser against the built app to check the real UI and screenshots, including mobile.

## Where it got things wrong (and how it was caught)

| Mistake | How it was found | Fix |
|---|---|---|
| Created folders with brace expansion in `sh`, so the frontend `src` folders were never made | A later write failed ("Directory nonexistent") | Recreated the folders, rewrote the files |
| Stop-word patch for FAQ retrieval silently did not apply after auto-formatting | A bare "no" matched the cancellation FAQ; the stats test failed | Rewrote the block and checked the result |
| A birth date said with the request ("born May 17 1995") was parsed as the appointment date | The "everything in one sentence" test | `strip_dob` before slot extraction |
| "new patient visit" triggered new-patient registration for established patients | Dialogue test | Negative look-ahead in the regex |
| "march 4th" not recognised as a birth date without a year (`\b` after the digit) | Unit test | Allow the ordinal suffix |
| Static-file route annotation crashed FastAPI when the built UI existed | Starting the real server with the built UI (unit tests had no UI folder) | `response_model=None`, plus a test with a static dir |
| Several tests had wrong expectations (counted a seeded past visit, assumed load-balancing picks every provider) | Failing tests; the code was right | Corrected the tests |
| First Docker/deploy files were written without a Docker binary available | Stated limitation | Verified the equivalent prod-mode run and Postgres separately; the Docker build itself is checked by CI |
| Hardening round: golden scenarios had wrong expectations (a doctor who only works Friday mornings; a verified patient correctly skipping re-verification) | The runner failed 2 of 38 | The assistant was right; the scenarios were corrected |
| A browser test counted another test's booking as a duplicate (shared server) | Failing e2e test | Scoped the assertion; the proof of idempotency is the replayed confirmation card |
| Calendar used `role="grid"` without rows and cells; transcript was a scrollable region with no keyboard access | axe accessibility checks in the browser tests | Used a plain group; made the scroll regions focusable |
| `npm audit` found a vulnerable runtime dependency (react-router) and dev-tool issues | Ran the audit | Upgraded react-router-dom, vite and vitest; re-ran typecheck, unit, build and all browser tests |
| Multi-worker start-up would have run migrations and seeding in every worker at once | Reasoning while load-testing | Postgres advisory lock; verified with 4 workers on Postgres |
| Read a 4-worker Postgres load result as a regression | Checked `nproc`: the sandbox has one CPU | Documented that the number is meaningless and what was and wasn't measured |
| A lint failure (nested `with`) that CI would have rejected | Ran the full lint over every folder | Fixed |

## Was an "agent" or LangGraph used?

The *assistant that wrote this code* worked agentically in a sandbox: it wrote code, ran tests, read failures and fixed them,
which is why the bug table above is long. The *product* contains no model and no agent framework: the task forbids an AI model
making decisions, and LangGraph orchestrates model calls, so it would add a dependency and no benefit. The explicit state machine
with typed state is the deterministic equivalent. Where AI could legitimately help is offline (for example drafting extra test phrasings
or reviewing translations), never at run time.

## Round 3 (Urdu, waitlist, silence, delivery): more mistakes

| Mistake | How it was found | Fix |
|---|---|---|
| Several patch scripts aborted half-way when one text pattern had been re-formatted by the linter, leaving the codebase partly patched | Tests failed in confusing ways; some patches had silently not been applied | Re-read the real code, re-applied each patch with an assertion, and re-ran everything |
| Urdu tests "passed" because the success words also appear in the help message | Reviewing why "haan" worked when it should not have | Tests now assert the database outcome, not the wording |
| "No, 3 pm" was parsed as *not* 3 pm (also in English) | An Urdu correction scenario | Comma-aware correction handling plus a leading-"no" rule |
| Phonetic key broke for names starting with a vowel sound (Usman/عثمان, O'Connor/اوکونر) | Transliteration unit test | Initial vowels count as the same sound |
| Overlapping appointments were drawn on top of each other in the calendar | A browser test could not click an element that was covered | Side-by-side lanes, with a unit test |
| UI showed "Patient receives…" before the table refreshed; Appointments tab rendered an empty table while loading | Flaky browser tests | Refresh first; show a loading state |
| Browser tests depended on the real clock (demo data, "tomorrow" landing on a Saturday, seeded visits colliding) | They passed at one time of day and failed at another | The e2e server runs on a pinned clock; the suite passes at five different times |
| Wrote Urdu wording and number words from memory | Cannot be verified by the assistant | Spot-checked number words in a test; **a native speaker must review the Urdu** |
| The sandbox's storage failed once mid-session | The tool reported it | Recovered from the delivered zip and the working copy; nothing was lost |

## Review and verification done by a human

*(Fill this in with what you personally reviewed, changed, or tested.)*

## Token usage

**TO FILL IN before submitting.** The task asks for token logs or screenshots from the tools used. The
assistant cannot see its own token counts, so none are claimed here. Add screenshots of the usage page for
the Claude account/conversation used, and paste the totals below.

![alt text](image-1.png)

An Estimate
~120,000 input tokens, ~40,000 output tokens