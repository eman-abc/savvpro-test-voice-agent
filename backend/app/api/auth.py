"""Staff sign-in: bcrypt passwords, JWT in an HttpOnly cookie, CSRF double-submit token."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.api.deps import AppContext, client_ip, get_ctx, get_db, require_staff
from app.api.schemas import LoginIn
from app.security import (
    AUTH_COOKIE,
    CSRF_COOKIE,
    create_token,
    hash_password,
    new_csrf_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
_DUMMY_HASH = hash_password("not-a-real-password", 4)  # keeps timing similar for unknown users


@router.post("/login")
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    key = f"{client_ip(request)}|{body.username.lower()}"
    if ctx.throttle.is_locked(key):
        raise HTTPException(
            429, f"Too many failed attempts. Try again in {ctx.settings.login_lockout_minutes} minutes."
        )
    user = db.scalar(select(m.StaffUser).where(m.StaffUser.username == body.username))
    ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        ctx.throttle.record_failure(key)
        raise HTTPException(401, "Incorrect username or password")
    ctx.throttle.reset(key)
    s = ctx.settings
    max_age = s.jwt_expire_minutes * 60
    jti = str(uuid.uuid4())
    db.add(m.StaffSession(jti=jti, user_id=user.id, expires_at=m.utcnow() + timedelta(seconds=max_age)))
    db.query(m.StaffSession).filter(m.StaffSession.expires_at < m.utcnow()).delete()  # tidy expired rows
    db.commit()
    response.set_cookie(
        AUTH_COOKIE,
        create_token(s, user.username, user.role, jti),
        max_age=max_age,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, new_csrf_token(), max_age=max_age, httponly=False, secure=s.cookie_secure, samesite="lax", path="/"
    )
    return {"username": user.username, "role": user.role}


@router.post("/logout")
def logout(
    request: Request, response: Response, db: Session = Depends(get_db), _user: m.StaffUser = Depends(require_staff)
) -> dict:
    session = db.get(m.StaffSession, request.state.session_jti)
    if session:
        session.revoked_at = m.utcnow()  # the cookie is also cleared, but a copied token is now dead too
        db.commit()
    response.delete_cookie(AUTH_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: m.StaffUser = Depends(require_staff)) -> dict:
    return {"username": user.username, "role": user.role}
