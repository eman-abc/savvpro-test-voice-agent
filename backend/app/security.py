"""Password hashing, JWT cookies, CSRF token and login throttling."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import Settings

AUTH_COOKIE = "clinic_session"
CSRF_COOKIE = "clinic_csrf"


def hash_password(password: str, rounds: int) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_token(settings: Settings, username: str, role: str, jti: str) -> str:
    exp = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": username, "role": role, "exp": exp, "jti": jti}, settings.jwt_secret, algorithm="HS256")


def decode_token(settings: Settings, token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class LoginThrottle:
    """In-memory lockout after repeated failures. Use Redis when running multiple replicas."""

    max_failures: int
    lockout_seconds: int
    _fails: dict[str, list[float]] = field(default_factory=dict)

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.lockout_seconds
        self._fails[key] = [t for t in self._fails.get(key, []) if t > cutoff]
        return self._fails[key]

    def is_locked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_failures

    def record_failure(self, key: str) -> None:
        self._recent(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


@dataclass
class RateLimiter:
    """Sliding one-minute window per client key."""

    per_minute: int
    _hits: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if t > now - 60]
        if len(hits) >= self.per_minute:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > 5000:  # keep memory bounded
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - 60}
        return True
