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

## Round 4 (WhatsApp, voice notes, speech): more mistakes

| Mistake | How it was found | Fix |
|---|---|---|
| The staff "Send due reminders" button and the background loop used the log-only sender, so reminders would never have gone out over WhatsApp | The first WhatsApp reminder test failed | Both now use the configured notifier |
| Webhook de-duplication was check-then-insert, so a retry racing the original sent the patient an apology | Driving the real server on Postgres with six simultaneous deliveries; the SQLite tests are serial and could not show it | Insert-first on the primary key, plus a regression test |
| A patch script aborted halfway again because the linter had reformatted its target | Tests failed in odd ways | Re-read the real code and re-applied each change with an assertion |
| A reminder that could not be sent (window closed) would have been retried and re-recorded every minute | Reasoning about the background loop | Back-off, a cap of three attempts, and distinct statuses |
| Browser tests selected a dropdown option by an exact label that includes a phone suffix | A failing test | Select by value |
| `strftime("%-d")` in the demo-patient hints: valid on Linux, a crash on Windows. Every test passed in my Linux sandbox and CI | You ran the suite on Windows and 8 tests failed | Portable formatting, plus a test that scans the code for platform-specific format codes. **Lesson: I only ever ran things on Linux.** |
| Voice-note and speech code was written against Meta's and Deepgram's documented formats from memory | Cannot be verified here | Tested against fakes that record every request; **needs a first run with real accounts** |

## Round 5 (knowledge layer, graph, agent): mistakes and honest gaps

| Mistake | How it was found | Fix |
|---|---|---|
| The emergency matcher missed real phrasings ("pain in my chest", "he is not breathing", Urdu with an adjective in the middle), **in the original rules engine too** | Writing an Urdu emergency test for the agent failed, and the failing reply came from the rules engine | Shared proximity matcher, more keywords, an Urdu list, 36 phrasing tests, a parity test and a mutation check |
| New ingestion tests mutated a cached rules object and silently broke unrelated tests | They passed alone and failed in the full run | Each test gets a private deep copy |
| Installing LangChain's client pulled in `httpx2`, which changed Starlette's test client; an old cookie test depended on cookie ordering | The test failed in the original repo too | The test no longer depends on ordering |
| `numpy` was used but not declared | A clean virtualenv install | Declared in the requirements |
| Three of my tests passed for the wrong reasons (an `or True`, a loose fallback, a `raise` inside `pytest.raises`) and a later assertion leaned on operator precedence | Re-reading them, then breaking the code to see if they failed | Rewritten; mutation checks |
| Patch scripts aborted halfway after the linter reformatted their targets, again | Odd test behaviour | Rewrite whole functions by name instead of matching exact text |
| The dense model collapsed on Urdu typed with Arabic letters (0.63 to 0.32) and the hybrids inherited it | The robustness probe I added to the evaluation | Fold Urdu letters before embedding |
| I assumed Elasticsearch deletes indices by wildcard, and that a handoff call contains the word "person" | Integration fixture errors; a failing test | Delete by name; search for text from a real call |
| I claimed "the model is never called" for a scenario where it legitimately handles the first turn | A failing test | The claim is now about the emergency turn |
| An ambiguous Playwright selector ("Load" also matched "Reload") | Playwright's strict mode | Exact match |
| **Not fixed, only flagged:** the agent has never run against a live LLM; the Docker-based CI job and docker-compose were never run; Python 3.14 compatibility of the new dependencies is untested | n/a | Stated in the README and DESIGN |

## Review and verification done by a human

*(Fill this in with what you personally reviewed, changed, or tested.)*

## Token usage

**TO FILL IN before submitting.** The task asks for token logs or screenshots from the tools used. The
assistant cannot see its own token counts, so none are claimed here. Add screenshots of the usage page for
the Claude account/conversation used, and paste the totals below.

![alt text](image-1.png)

An Estimate
~120,000 input tokens, ~40,000 output tokens