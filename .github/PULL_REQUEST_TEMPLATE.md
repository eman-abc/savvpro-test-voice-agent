## What and why

## Checklist
- [ ] `pytest tests` passes (and `pytest e2e` if the UI changed)
- [ ] New reply text has an Urdu translation (a test fails if not) and was reviewed by a native speaker
- [ ] New behaviour has a unit test, and a golden scenario in `backend/scenarios` if it is something a caller can do
- [ ] Database changes come with an Alembic migration (`alembic revision --autogenerate`)
- [ ] No secrets, no real personal data
