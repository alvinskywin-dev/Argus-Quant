"""
Sprint 20A — low-level auth primitives.

Pure functions only (no DB, no FastAPI). Covers:
  - password hashing / verification (bcrypt via passlib)
  - JWT access tokens (HS256 via python-jose)
  - opaque refresh / one-time tokens (secrets) + sha256 hashing for storage
  - TOTP 2FA helpers (pyotp)
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import bcrypt
import pyotp
from jose import JWTError, jwt

from app.config import settings

# bcrypt only consumes the first 72 *bytes* of a password and raises on more.
# We use the bcrypt library directly rather than passlib, whose bcrypt-4.x
# backend version-detection is broken and corrupts normal hashing.
_BCRYPT_MAX_BYTES = 72


# ── passwords ─────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt(rounds=settings.bcrypt_rounds)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        pw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(pw, password_hash.encode("utf-8"))
    except Exception:
        return False


# ── JWT access tokens ─────────────────────────────────────────────


class TokenError(Exception):
    """Raised when a JWT cannot be decoded / verified."""


def _signing_key() -> str:
    key = settings.jwt_signing_key
    if not key:
        # Never silently sign with an empty key in production.
        key = "dev-insecure-secret-change-me"
    return key


def create_access_token(
    subject: str | int,
    role: str,
    *,
    extra: Optional[dict[str, Any]] = None,
    ttl_minutes: Optional[int] = None,
) -> str:
    now = datetime.now(timezone.utc)
    ttl = ttl_minutes if ttl_minutes is not None else settings.access_token_ttl_min
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": now + timedelta(minutes=ttl),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _signing_key(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, _signing_key(), algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("type") != "access":
        raise TokenError("not an access token")
    return payload


# ── opaque tokens (refresh / verify / reset) ──────────────────────


def generate_opaque_token(nbytes: int = 48) -> str:
    """Return a URL-safe random token to hand to the client (store the hash)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """sha256 hex digest — what we persist for refresh/verify/reset tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── TOTP 2FA ──────────────────────────────────────────────────────


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=settings.auth_issuer)


def verify_totp(secret: Optional[str], code: str) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)
    except Exception:
        return False
