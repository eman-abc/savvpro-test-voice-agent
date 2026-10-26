# Changelog

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
