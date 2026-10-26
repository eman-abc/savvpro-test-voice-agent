"""Database engine and session management (SQLite for dev/demo, Postgres for prod)."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings


def make_engine(url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("postgres://"):  # some hosts still hand out the legacy scheme
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        else:
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_pragma(dbapi_conn, _):  # enforce foreign keys + sane concurrency on SQLite
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


class Database:
    def __init__(self, settings: Settings):
        self.engine = make_engine(settings.database_url)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    def session(self) -> Session:
        return self.session_factory()

    def dependency(self) -> Iterator[Session]:
        db = self.session()
        try:
            yield db
        finally:
            db.close()


STARTUP_LOCK_ID = 727274


@contextmanager
def startup_lock(engine: Engine) -> Iterator[None]:
    """Serialise start-up work (migrations, seeding) across workers/replicas.

    On Postgres this is a session-level advisory lock, so only one process runs migrations and seeds at a
    time and the others wait, then find the work already done. SQLite is single-process here, so it's a no-op.
    """
    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": STARTUP_LOCK_ID})
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": STARTUP_LOCK_ID})
            conn.commit()
