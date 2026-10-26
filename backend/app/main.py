"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import models as m
from app.api import auth, calls, messages, staff
from app.api.deps import AppContext, client_ip, make_service
from app.brain.retrieval import FaqIndex
from app.config import Settings, get_settings
from app.db import Database, startup_lock
from app.logging_config import configure_logging
from app.metrics import Metrics
from app.migrate import upgrade as run_migrations
from app.reminders import LogNotifier, send_due_reminders
from app.rules import load_rules
from app.security import LoginThrottle, RateLimiter
from app.seed import ensure_staff, public_demo_hints, purge_old_calls, seed_demo

log = logging.getLogger("app")

CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def _init_sentry(settings: Settings) -> None:
    """Optional error reporting: set SENTRY_DSN and `pip install sentry-sdk`. No personal data is sent."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn, send_default_pii=False, traces_sample_rate=0.0, environment=settings.app_env
        )
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    rules = load_rules(settings.rules_path)
    db = Database(settings)
    ctx = AppContext(
        settings=settings,
        db=db,
        rules=rules,
        faq=FaqIndex(rules.faq),
        throttle=LoginThrottle(settings.login_max_failures, settings.login_lockout_minutes * 60),
        limiter=RateLimiter(settings.rate_limit_per_minute),
        metrics=Metrics(),
    )
    _init_sentry(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with startup_lock(db.engine):
            if settings.use_migrations:
                run_migrations(settings.database_url)
            else:
                m.Base.metadata.create_all(db.engine)
            with db.session() as s:
                svc = make_service(s, settings, rules)
                svc.sync_providers()
                ensure_staff(s, settings)
                if settings.demo_mode:
                    seed_demo(svc)
                purged = purge_old_calls(s, settings.session_retention_hours)
                log.info("startup complete (purged %d old calls)", purged)
        task = asyncio.create_task(_background_loop()) if settings.background_enabled else None
        yield
        if task:
            task.cancel()

    async def _background_loop() -> None:
        """Every minute: expire unanswered waitlist offers (and pass the slot on); send due reminders if enabled."""
        notifier = LogNotifier()

        def once() -> int:
            with db.session() as s:
                svc = make_service(s, settings, rules)
                n = svc.waitlist.expire()  # type: ignore[union-attr]
                if settings.reminders_enabled:
                    n += send_due_reminders(svc, notifier, settings.reminder_hours)
                return n

        while True:
            try:
                await asyncio.to_thread(once)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("reminder pass failed")
            await asyncio.sleep(60)

    app = FastAPI(
        title="Clinic Voice Assistant",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.ctx = ctx

    if settings.cors_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_list,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-CSRF-Token"],
        )

    @app.middleware("http")
    async def request_pipeline(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        if request.url.path.startswith("/api/") and not ctx.limiter.allow(f"ip:{client_ip(request)}"):
            response = JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = CSP
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        template = getattr(route, "path", "unmatched")
        ctx.metrics.inc(
            "http_requests_total", {"method": request.method, "route": template, "status": str(response.status_code)}
        )
        ctx.metrics.observe("http_request_seconds", time.perf_counter() - started, {"route": template})
        log.info(
            "request",
            extra={
                "request_id": rid,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000),
            },
        )
        return response

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> PlainTextResponse:
        token = settings.metrics_token
        if token:
            if request.headers.get("authorization") != f"Bearer {token}":
                return PlainTextResponse("unauthorized\n", status_code=401)
        elif settings.app_env == "prod":
            return PlainTextResponse("not found\n", status_code=404)  # never expose unauthenticated in prod
        return PlainTextResponse(ctx.metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/api/version", tags=["public"])
    def version() -> dict:
        return {
            "version": "1.2.0",
            "commit": os.environ.get("GIT_SHA") or os.environ.get("RENDER_GIT_COMMIT") or "dev",
            "built": os.environ.get("BUILD_TIME", "unknown"),
        }

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        try:
            with db.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return JSONResponse({"status": "ready"})
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "database unavailable"}, status_code=503)

    @app.get("/api/config", tags=["public"])
    def public_config() -> dict:
        c = rules.clinic
        cfg: dict = {
            "clinic": {"name": c.name, "phone": c.phone, "address": c.address, "timezone": c.timezone},
            "demo_mode": settings.demo_mode,
            "services": [{"key": s.key, "name": s.name, "minutes": s.duration_minutes} for s in rules.services],
            "policy": {"cancel_notice_hours": rules.policy.cancel_notice_hours},
        }
        if settings.demo_mode:
            cfg["demo_patients"] = public_demo_hints()
            cfg["demo_staff"] = {"username": settings.staff_username, "password": settings.staff_password}
        return cfg

    app.include_router(calls.router)
    app.include_router(auth.router)
    app.include_router(staff.router)
    app.include_router(messages.router)

    static = Path(settings.static_dir)
    if (static / "index.html").exists():
        if (static / "assets").exists():
            app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
        def spa(full_path: str) -> FileResponse | JSONResponse:
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            candidate = (static / full_path).resolve()
            if full_path and candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static / "index.html")

    return app
