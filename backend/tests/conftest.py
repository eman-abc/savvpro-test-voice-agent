from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.brain.dialog import Brain
from app.brain.retrieval import FaqIndex
from app.brain.state import DialogState
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.models import Base
from app.rules import load_rules
from app.seed import seed_demo
from app.services.clinic import ClinicService

NOW = datetime(2026, 10, 26, 10, 0)  # a Monday, 10:00 clinic time
RULES_PATH = "config/clinic.yaml"


def make_settings(**over) -> Settings:
    base = dict(
        app_env="test",
        database_url="sqlite:///:memory:",
        bcrypt_rounds=4,
        fixed_now=NOW.isoformat(),
        demo_mode=True,
        staff_username="reception",
        staff_password="test-pass-123",
        rate_limit_per_minute=100000,
        jwt_secret="t" * 32,
        background_enabled=False,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def rules():
    return load_rules(RULES_PATH)


@pytest.fixture
def svc(rules):
    """A seeded in-memory database with the clock pinned to NOW."""
    db = Database(make_settings())
    Base.metadata.create_all(db.engine)
    session = db.session()
    service = ClinicService(session, rules, now_fn=lambda: NOW)
    service.sync_providers()
    seed_demo(service, NOW)
    yield service
    session.close()


class Call:
    """Drives the brain like a caller would, one utterance at a time."""

    def __init__(self, svc: ClinicService, rules):
        self.svc, self.state = svc, DialogState()
        self.brain = Brain(svc, FaqIndex(rules.faq), "test:call")
        self.last = self.brain.start(self.state)

    def say(self, text: str) -> str:
        self.last = self.brain.handle(self.state, text)
        return self.last.reply

    @property
    def step(self) -> str:
        return self.state.step

    @property
    def trace(self) -> dict:
        return self.last.trace


@pytest.fixture
def call(svc, rules):
    return Call(svc, rules)


@pytest.fixture
def client():
    with TestClient(create_app(make_settings())) as c:
        yield c


def login(client: TestClient, username="reception", password="test-pass-123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    return r


def csrf(client: TestClient) -> dict:
    return {"X-CSRF-Token": client.cookies.get("clinic_csrf", "")}
