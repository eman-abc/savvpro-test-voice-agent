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
    # --- WhatsApp (Meta Cloud API). The channel is enabled only when all four credentials are set. ---
    whatsapp_verify_token: str | None = None  # you choose it; Meta echoes it back during webhook set-up
    whatsapp_app_secret: str | None = None  # signs every webhook call (X-Hub-Signature-256)
    whatsapp_access_token: str | None = None  # System User token (the temporary one expires in 24 hours)
    whatsapp_phone_number_id: str | None = None
    whatsapp_api_version: str = "v21.0"
    whatsapp_graph_base: str = "https://graph.facebook.com"
    whatsapp_allowed_numbers: str = ""  # comma-separated digits; empty = anyone who can reach the number
    whatsapp_template_name: str | None = None  # approved template used when the 24-hour window is closed
    demo_whatsapp_number: str | None = None  # links the demo patient below to YOUR number at start-up (never committed)
    demo_whatsapp_patient: str = "Wei Zhang"
    # --- speech-to-text (pretrained model behind an interface; the browser recogniser is the fallback) ---
    stt_provider: str = "none"  # none | deepgram | fake (fake is for tests and demos without a key)
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3"
    deepgram_base: str = "https://api.deepgram.com"
    asr_min_confidence: float = 0.55  # below this the assistant reads back what it heard before acting
    stt_max_bytes: int = 5_000_000
    fake_stt_text: str = "what time do you open"
    # --- knowledge layer: search and graph. Everything here is optional; without it the app uses its own BM25. ---
    elasticsearch_url: str | None = None  # e.g. http://localhost:9200
    elasticsearch_api_key: str | None = None
    es_index_prefix: str = "clinic"
    es_urdu_analyzer: str = "urdu_custom"  # standard | arabic | persian | urdu_custom (see docs/RETRIEVAL_EVAL.md)
    dense_enabled: bool = False  # multilingual embeddings + hybrid fusion (downloads a ~220 MB model on first use)
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    neo4j_uri: str | None = None  # e.g. bolt://localhost:7687 or neo4j+s://<id>.databases.neo4j.io
    neo4j_user: str = "neo4j"
    neo4j_password: str | None = None
    # --- agent brain (LangGraph + an LLM). "rules" keeps the deterministic engine. ---
    brain: str = "rules"  # rules | agent
    llm_provider: str = "none"  # none | groq | ollama | openai | fake
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    agent_max_steps: int = 6
    background_enabled: bool = True  # expire waitlist offers / send reminders every minute
    reminders_enabled: bool = False
    reminder_hours: int = 24
    inbound_secret: str | None = None  # shared secret for POST /api/inbound/message (unset = endpoint disabled)
    waitlist_offer_minutes: int = 30
    verify_lock_failures: int = 5  # failed identity checks per name before it is locked
    verify_lock_minutes: int = 15
    fixed_now: str | None = None  # test-only clock override, honoured only when APP_ENV=test

    @model_validator(mode="after")
    def _check_speech(self) -> "Settings":
        if self.stt_provider == "deepgram" and not self.deepgram_api_key:
            raise ValueError("STT_PROVIDER=deepgram needs DEEPGRAM_API_KEY")
        if self.stt_provider not in ("none", "deepgram", "fake"):
            raise ValueError("STT_PROVIDER must be none, deepgram or fake")
        if self.app_env == "prod" and self.stt_provider == "fake":
            raise ValueError("STT_PROVIDER=fake is not allowed in prod")
        return self

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.app_env == "prod":
            if self.jwt_secret == _INSECURE_DEFAULT_SECRET or len(self.jwt_secret) < 24:
                raise ValueError("JWT_SECRET must be set to a random value of 24+ chars in prod")
            if not self.demo_mode and self.staff_password == "demo-staff-2026":
                raise ValueError("STAFF_PASSWORD must be changed when DEMO_MODE=false")
        return self

    @property
    def whatsapp_enabled(self) -> bool:
        return all(
            (
                self.whatsapp_verify_token,
                self.whatsapp_app_secret,
                self.whatsapp_access_token,
                self.whatsapp_phone_number_id,
            )
        )

    @property
    def whatsapp_allowed(self) -> set[str]:
        return {d for n in self.whatsapp_allowed_numbers.split(",") if (d := "".join(c for c in n if c.isdigit()))}

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
