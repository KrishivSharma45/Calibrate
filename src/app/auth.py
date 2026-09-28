"""Who is making this request, and are they allowed to.

Every request passes through two app-wide dependencies (see main.py):
  attach_auth   -> reads the session cookie, sets request.state.user / .session
  csrf_protect  -> refuses unsafe non-JSON requests without a valid CSRF token

Routes then declare what they need:
  Depends(require_user)                        401 if not logged in
  Depends(require_role(Role.organizer, ...))   401 if not logged in, 403 if wrong role
Object-level checks (e.g. "is this *your* score") live in the route/service
code on top of these, because a role alone cannot answer them.
"""

import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as DbSession

from app.config import COOKIE_SECURE
from app.db import get_db
from app.models import Role, Session, User, utcnow
from app.security import SESSION_LIFETIME, hash_token

COOKIE_NAME = "session"
ANON_CSRF_COOKIE = "csrf_anon"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
STAFF = (Role.organizer, Role.admin)


# --- identity ----------------------------------------------------------------


def attach_auth(request: Request, db: DbSession = Depends(get_db)) -> None:
    request.state.session = None
    request.state.user = None
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return
    session = db.get(Session, hash_token(token))
    if session is None:
        return
    if session.expires_at <= utcnow():
        db.delete(session)
        db.commit()
        return
    request.state.session = session
    request.state.user = session.user


def current_user(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def require_role(*roles: Role):
    allowed = set(roles)

    def dependency(user: User = Depends(require_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Not allowed for your role")
        return user

    return dependency


# --- cookies -----------------------------------------------------------------


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(COOKIE_NAME, token, max_age=int(SESSION_LIFETIME.total_seconds()),
                        httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


# --- CSRF --------------------------------------------------------------------


def csrf_token_for(request: Request) -> tuple[str, bool]:
    """Token to embed in forms, and whether a new anonymous cookie must be set.

    Logged in: the per-session token stored server-side.
    Anonymous (login/register forms): a double-submit cookie. Another site can
    neither read nor set our cookie, so it cannot produce a matching pair.
    """
    session = getattr(request.state, "session", None)
    if session is not None:
        return session.csrf_token, False
    existing = request.cookies.get(ANON_CSRF_COOKIE)
    if existing:
        return existing, False
    return secrets.token_urlsafe(32), True


def set_anon_csrf_cookie(response, token: str) -> None:
    response.set_cookie(ANON_CSRF_COOKIE, token, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, path="/")


async def csrf_protect(request: Request, _auth: None = Depends(attach_auth)) -> None:
    if request.method not in UNSAFE_METHODS:
        return
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        # A cross-site page cannot send this content type without a CORS
        # preflight, and we never answer preflights, so JSON is not forgeable.
        return
    session = getattr(request.state, "session", None)
    expected = session.csrf_token if session else request.cookies.get(ANON_CSRF_COOKIE)
    sent = request.headers.get("x-csrf-token")
    if sent is None and content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
        sent = (await request.form()).get("csrf_token")
    if not expected or not sent or not hmac.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


# --- login throttling ----------------------------------------------------------


class LoginThrottle:
    """Counts failed logins per key in a sliding window (in memory, per process)."""

    def __init__(self, max_failures: int = 10, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window = window_seconds
        self.failures: dict[str, deque] = defaultdict(deque)

    def _prune(self, key: str) -> deque:
        q = self.failures[key]
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        return any(len(self._prune(k)) >= self.max_failures for k in keys)

    def fail(self, *keys: str) -> None:
        for k in keys:
            self._prune(k).append(time.monotonic())

    def reset(self, *keys: str) -> None:
        for k in keys:
            self.failures.pop(k, None)


login_throttle = LoginThrottle()


class RateLimit(LoginThrottle):
    """Same sliding window, counting every action instead of failures."""

    def allow(self, key: str) -> bool:
        if self.blocked(key):
            return False
        self.fail(key)
        return True


vote_limit = RateLimit(max_failures=30, window_seconds=600)     # 30 votes / 10 min / user
comment_limit = RateLimit(max_failures=5, window_seconds=60)    # 5 comments / min / user


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow local paths after login, never //evil.com or https://evil.com."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    return target
