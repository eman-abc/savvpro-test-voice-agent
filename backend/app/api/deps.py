"""Shared FastAPI dependencies and the application context."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.brain.retrieval import FaqIndex
from app.config import Settings
from app.db import Database
from app.metrics import Metrics
from app.rules import ClinicRules
from app.security import AUTH_COOKIE, CSRF_COOKIE, LoginThrottle, RateLimiter, decode_token
from app.services.clinic import ClinicService
from app.services.engagement import LogNotifier, Outbox, WaitlistService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    rules: ClinicRules
    faq: FaqIndex
    throttle: LoginThrottle
    limiter: RateLimiter
    metrics: Metrics


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def get_db(ctx: AppContext = Depends(get_ctx)) -> Iterator[Session]:
    db = ctx.db.session()
    try:
        yield db
    finally:
        db.close()


def attach_engagement(svc: ClinicService, settings: Settings) -> None:
    """Give a service the waitlist, and make every freed slot go to the next person in line."""
    wl = WaitlistService(svc, Outbox(svc.db, LogNotifier()), settings.waitlist_offer_minutes)
    svc.waitlist = wl
    svc.on_freed = lambda freed: wl.on_slot_freed(freed)


def make_service(db: Session, settings: Settings, rules: ClinicRules) -> ClinicService:
    """Builds the domain service. `FIXED_NOW` is a test-only clock seam (ignored outside APP_ENV=test)."""
    now_fn = None
    if settings.app_env == "test" and settings.fixed_now:
        fixed = datetime.fromisoformat(settings.fixed_now)
        now_fn = lambda: fixed  # noqa: E731
    svc = ClinicService(db, rules, now_fn)
    attach_engagement(svc, settings)
    return svc


def get_service(db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)) -> ClinicService:
    return make_service(db, ctx.settings, ctx.rules)


def client_ip(request: Request) -> str:
    """Caller address. X-Forwarded-For is only believed for the number of proxies we run behind
    (TRUSTED_PROXY_COUNT): each of them appends the address it saw, so the real client is that many
    entries from the right. Anything a client put further left is ignored, so it cannot be spoofed."""
    n = request.app.state.ctx.settings.trusted_proxy_count
    fwd = request.headers.get("x-forwarded-for")
    if n > 0 and fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if len(parts) >= n:
            return parts[-n]
    return request.client.host if request.client else "unknown"


UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def require_staff(request: Request, db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)) -> m.StaffUser:
    token = request.cookies.get(AUTH_COOKIE)
    claims = decode_token(ctx.settings, token) if token else None
    if not claims:
        raise HTTPException(401, "Not signed in")
    if request.method in UNSAFE:
        header, cookie = request.headers.get("x-csrf-token"), request.cookies.get(CSRF_COOKIE)
        if not header or not cookie or header != cookie:
            raise HTTPException(403, "Missing or invalid CSRF token")
    user = db.scalar(select(m.StaffUser).where(m.StaffUser.username == claims["sub"]))
    session = db.get(m.StaffSession, claims.get("jti", ""))
    if (
        user is None
        or session is None
        or session.user_id != user.id
        or session.revoked_at
        or session.expires_at < m.utcnow()
    ):
        raise HTTPException(401, "Session ended. Please sign in again.")
    request.state.session_jti = session.jti
    return user


def require_admin(user: m.StaffUser = Depends(require_staff)) -> m.StaffUser:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user
