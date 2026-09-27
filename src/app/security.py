"""Password hashing and session tokens."""

import hashlib
import os
import secrets
from datetime import datetime, timedelta

import bcrypt
from sqlalchemy.orm import Session as DbSession

from app.models import Session, User, utcnow

SESSION_LIFETIME = timedelta(days=7)
# bcrypt cost: 12 (~0.25s per hash) in real use. Tests lower it via the env var
# only to run fast; verify_password reads the cost from the stored hash.
BCRYPT_ROUNDS = int(os.environ.get("BCRYPT_ROUNDS", "12"))


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def hash_token(token: str) -> str:
    """Sessions are stored by SHA-256 of the cookie value, never the value itself.

    A plain hash (no salt, no bcrypt) is enough here: tokens are 256-bit random,
    so there is nothing to brute-force, and lookups must be fast.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    db: DbSession, user: User, token: str | None = None, expires_at: datetime | None = None
) -> str:
    """Create a session row and return the raw token for the cookie."""
    token = token or secrets.token_urlsafe(32)
    db.add(Session(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        expires_at=expires_at or utcnow() + SESSION_LIFETIME,
    ))
    return token
