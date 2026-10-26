"""Application settings, read from environment variables (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
_INSECURE_DEFAULT_SECRET = "dev-only-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"  # dev | test | prod
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'clinic.db'}"
    rules_path: Path = BASE_DIR / "config" / "clinic.yaml"
    scenarios_path: Path = BASE_DIR / "scenarios"
    static_dir: Path = BASE_DIR / "static"

    jwt_secret: str = _INSECURE_DEFAULT_SECRET
    jwt_expire_minutes: int = 480
    cookie_secure: bool = False
    bcrypt_rounds: int = 12

    staff_username: str = "reception"
    staff_password: str = "demo-staff-2026"

    # Demo mode: seeds fake patients/appointments and shows sign-in hints in the UI.
    demo_mode: bool = True
    cors_origins: str = ""  # comma separated, only needed when UI is served elsewhere

    rate_limit_per_minute: int = 120
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    call_idle_minutes: int = 30
    call_max_turns: int = 80
    session_retention_hours: int = 48
    log_level: str = "INFO"
    trusted_proxy_count: int = 0  # proxies in front of us that append to X-Forwarded-For (Render: 1)
    use_migrations: bool = False  # run Alembic at start-up instead of create_all (set true in prod)
    metrics_token: str | None = None  # bearer token for /metrics; unset = open in dev, disabled in prod
    sentry_dsn: str | None = None
    background_enabled: bool = True  # expire waitlist offers / send reminders every minute
    reminders_enabled: bool = False
    reminder_hours: int = 24
    inbound_secret: str | None = None  # shared secret for POST /api/inbound/message (unset = endpoint disabled)
    waitlist_offer_minutes: int = 30
    verify_lock_failures: int = 5  # failed identity checks per name before it is locked
    verify_lock_minutes: int = 15
    fixed_now: str | None = None  # test-only clock override, honoured only when APP_ENV=test

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.app_env == "prod":
            if self.jwt_secret == _INSECURE_DEFAULT_SECRET or len(self.jwt_secret) < 24:
                raise ValueError("JWT_SECRET must be set to a random value of 24+ chars in prod")
            if not self.demo_mode and self.staff_password == "demo-staff-2026":
                raise ValueError("STAFF_PASSWORD must be changed when DEMO_MODE=false")
        return self

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
