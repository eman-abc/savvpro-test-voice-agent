# Contributing

```bash
make install        # backend venv + frontend deps
make test           # backend tests + frontend unit tests
make lint
make e2e            # real-browser tests (builds the UI first)
pre-commit install  # ruff, gitleaks and basic hygiene on every commit
```

Rules of the house:
1. **The brain stays deterministic.** No AI model decides anything at run time.
2. **Every reply is a template with an Urdu translation** (`backend/app/brain/messages_ur.py`). A test fails otherwise.
3. **Every caller-visible behaviour gets a golden scenario** (`backend/scenarios/*.yaml`), in English and Urdu where it applies.
4. **Schema changes need a migration**; a test fails if models and migrations disagree.
5. Tests must not depend on the real clock. Use the pinned clock (`FIXED_NOW`, `NOW` in `tests/conftest.py`).
