.PHONY: install test lint api ui build docker e2e loadtest audit scenarios migrate
install:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
	cd frontend && npm ci
test:
	cd backend && . .venv/bin/activate && pytest --cov=app --cov-report=term-missing
	cd frontend && npm test
lint:
	cd backend && . .venv/bin/activate && ruff check app tests && ruff format --check app tests
	cd frontend && npm run typecheck
api:
	cd backend && . .venv/bin/activate && uvicorn app.asgi:app --reload --port 8000
ui:
	cd frontend && npm run dev
build:
	cd frontend && npm run build && rm -rf ../backend/static && cp -r dist ../backend/static
docker:
	docker compose up --build
e2e: build
	cd backend && . .venv/bin/activate && playwright install chromium && pytest e2e
loadtest:
	cd backend && . .venv/bin/activate && python loadtest/loadtest.py --base-url http://localhost:8000 --callers 20 --seconds 20
audit:
	cd backend && . .venv/bin/activate && pip-audit -r requirements.txt
	cd frontend && npm audit --audit-level=high
scenarios:
	cd backend && . .venv/bin/activate && pytest tests/test_scenarios.py -v
migrate:
	cd backend && . .venv/bin/activate && alembic upgrade head
