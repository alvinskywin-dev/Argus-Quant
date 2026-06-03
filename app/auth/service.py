"""
Sprint 20A — auth business logic.

All functions take an AsyncSession and operate on the auth_* tables.
They raise AuthError (mapped to HTTP status codes by the router) on any
expected failure; the router never has to know the table layout.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import security
from app.auth.email import send_password_reset_email, send_verification_email
from app.config import settings
from app.database.models import AuthSession, AuthToken, AuthUser, LoginHistory
from app.utils.logger import logger

VALID_ROLES = ("ADMIN", "PREMIUM", "FREE")
_VERIFY_TTL = timedelta(hours=24)
_RESET_TTL = timedelta(hours=1)


class AuthError(Exception):
    """Expected auth failure carrying an HTTP status code."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── lookups ───────────────────────────────────────────────────────


async def get_user_by_id(db: AsyncSession, user_id: int) -> Optional[AuthUser]:
    return await db.get(AuthUser, user_id)


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[AuthUser]:
    res = await db.execute(select(AuthUser).where(AuthUser.email == email.lower()))
    return res.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[AuthUser]:
    res = await db.execute(select(AuthUser).where(AuthUser.username == username))
    return res.scalar_one_or_none()


# ── registration ──────────────────────────────────────────────────


async def register(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    username: Optional[str] = None,
) -> AuthUser:
    email = email.strip().lower()
    if await get_user_by_email(db, email):
        raise AuthError(409, "Email already registered")
    if username and await get_user_by_username(db, username):
        raise AuthError(409, "Username already taken")

    # First account ever becomes ADMIN; everyone else is FREE.
    existing = await db.execute(select(AuthUser.id).limit(1))
    is_first = existing.first() is None

    user = AuthUser(
        email=email,
        username=username,
        password_hash=security.hash_password(password),
        role="ADMIN" if is_first else "FREE",
        status="ACTIVE",
        is_verified=not settings.email_verification_required,
    )
    db.add(user)
    await db.flush()  # populate user.id

    if settings.email_verification_required:
        token = await _issue_token(db, user.id, kind="VERIFY", ttl=_VERIFY_TTL)
        await send_verification_email(user.email, token)

    logger.info(f"[auth] registered user id={user.id} email={email} role={user.role}")
    return user


# ── login ─────────────────────────────────────────────────────────


async def authenticate(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    totp_code: Optional[str],
    ip: Optional[str],
    device: Optional[str],
) -> tuple[AuthUser, str, str]:
    """Return (user, access_token, refresh_token) or raise AuthError."""
    email = email.strip().lower()
    user = await get_user_by_email(db, email)

    if user is None or not security.verify_password(password, user.password_hash):
        await _record_login(db, user, email, ip, device, success=False, detail="bad_credentials")
        if user is not None:
            await _register_failure(db, user)
        raise AuthError(401, "Invalid email or password")

    if user.locked_until and user.locked_until > _now():
        await _record_login(db, user, email, ip, device, success=False, detail="locked")
        raise AuthError(423, "Account temporarily locked. Try again later.")

    if user.status == "SUSPENDED":
        await _record_login(db, user, email, ip, device, success=False, detail="suspended")
        raise AuthError(403, "Account suspended")

    if settings.email_verification_required and not user.is_verified:
        await _record_login(db, user, email, ip, device, success=False, detail="unverified")
        raise AuthError(403, "Email not verified")

    if user.totp_enabled:
        if not totp_code:
            await _record_login(db, user, email, ip, device, success=False, detail="2fa_required")
            raise AuthError(401, "TOTP code required")
        if not security.verify_totp(user.totp_secret, totp_code):
            await _record_login(db, user, email, ip, device, success=False, detail="2fa_invalid")
            await _register_failure(db, user)
            raise AuthError(401, "Invalid TOTP code")

    # success
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = _now()

    access = security.create_access_token(user.id, user.role)
    refresh = await _create_session(db, user, ip, device)
    await _record_login(db, user, email, ip, device, success=True, detail=None)
    logger.info(f"[auth] login id={user.id} email={email} ip={ip}")
    return user, access, refresh


async def _register_failure(db: AsyncSession, user: AuthUser) -> None:
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= settings.account_lockout_threshold:
        user.locked_until = _now() + timedelta(minutes=settings.account_lockout_minutes)
        logger.warning(f"[auth] account locked id={user.id} until {user.locked_until}")


# ── sessions / refresh ────────────────────────────────────────────


async def _create_session(
    db: AsyncSession, user: AuthUser, ip: Optional[str], device: Optional[str]
) -> str:
    raw = security.generate_opaque_token()
    sess = AuthSession(
        user_id=user.id,
        refresh_token_hash=security.hash_token(raw),
        ip=ip,
        device=(device or "")[:256] or None,
        expires_at=_now() + timedelta(days=settings.refresh_token_ttl_days),
    )
    db.add(sess)
    return raw


async def refresh_access_token(
    db: AsyncSession, *, refresh_token: str, ip: Optional[str], device: Optional[str]
) -> tuple[AuthUser, str]:
    sess = await _get_active_session(db, refresh_token)
    if sess is None:
        raise AuthError(401, "Invalid or expired refresh token")
    user = await db.get(AuthUser, sess.user_id)
    if user is None or user.status == "SUSPENDED":
        raise AuthError(401, "Account unavailable")
    sess.last_seen = _now()
    if ip:
        sess.ip = ip
    access = security.create_access_token(user.id, user.role)
    return user, access


async def logout(db: AsyncSession, *, refresh_token: str) -> None:
    res = await db.execute(
        select(AuthSession).where(
            AuthSession.refresh_token_hash == security.hash_token(refresh_token)
        )
    )
    sess = res.scalar_one_or_none()
    if sess is not None:
        sess.revoked = True


async def _get_active_session(db: AsyncSession, refresh_token: str) -> Optional[AuthSession]:
    res = await db.execute(
        select(AuthSession).where(
            AuthSession.refresh_token_hash == security.hash_token(refresh_token)
        )
    )
    sess = res.scalar_one_or_none()
    if sess is None or sess.revoked or sess.expires_at <= _now():
        return None
    return sess


async def list_sessions(db: AsyncSession, user_id: int) -> list[AuthSession]:
    res = await db.execute(
        select(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked == False)  # noqa: E712
        .order_by(AuthSession.last_seen.desc())
    )
    return list(res.scalars().all())


# ── email verification ────────────────────────────────────────────


async def verify_email(db: AsyncSession, *, token: str) -> AuthUser:
    rec = await _consume_token(db, token, kind="VERIFY")
    user = await db.get(AuthUser, rec.user_id)
    if user is None:
        raise AuthError(404, "User not found")
    user.is_verified = True
    if user.status == "PENDING":
        user.status = "ACTIVE"
    return user


# ── password reset ────────────────────────────────────────────────


async def request_password_reset(db: AsyncSession, *, email: str) -> None:
    user = await get_user_by_email(db, email.strip().lower())
    # Do not reveal whether the email exists.
    if user is None:
        logger.info(f"[auth] password reset requested for unknown email={email}")
        return
    token = await _issue_token(db, user.id, kind="RESET", ttl=_RESET_TTL)
    await send_password_reset_email(user.email, token)


async def reset_password(db: AsyncSession, *, token: str, new_password: str) -> AuthUser:
    rec = await _consume_token(db, token, kind="RESET")
    user = await db.get(AuthUser, rec.user_id)
    if user is None:
        raise AuthError(404, "User not found")
    user.password_hash = security.hash_password(new_password)
    user.failed_login_count = 0
    user.locked_until = None
    # Force re-login everywhere after a password reset.
    for sess in await list_sessions(db, user.id):
        sess.revoked = True
    return user


# ── 2FA (TOTP) ────────────────────────────────────────────────────


async def begin_2fa_setup(db: AsyncSession, user: AuthUser) -> tuple[str, str]:
    secret = security.generate_totp_secret()
    user.totp_secret = secret
    user.totp_enabled = False  # not active until a code is confirmed
    uri = security.totp_provisioning_uri(secret, user.email)
    return secret, uri


async def confirm_2fa(db: AsyncSession, user: AuthUser, code: str) -> None:
    if not user.totp_secret:
        raise AuthError(400, "Start 2FA setup first")
    if not security.verify_totp(user.totp_secret, code):
        raise AuthError(401, "Invalid TOTP code")
    user.totp_enabled = True


async def disable_2fa(db: AsyncSession, user: AuthUser, code: str) -> None:
    if not user.totp_enabled:
        raise AuthError(400, "2FA is not enabled")
    if not security.verify_totp(user.totp_secret, code):
        raise AuthError(401, "Invalid TOTP code")
    user.totp_enabled = False
    user.totp_secret = None


# ── one-time token helpers ────────────────────────────────────────


async def _issue_token(db: AsyncSession, user_id: int, *, kind: str, ttl: timedelta) -> str:
    raw = security.generate_opaque_token(32)
    db.add(
        AuthToken(
            user_id=user_id,
            kind=kind,
            token_hash=security.hash_token(raw),
            expires_at=_now() + ttl,
        )
    )
    return raw


async def _consume_token(db: AsyncSession, token: str, *, kind: str) -> AuthToken:
    res = await db.execute(
        select(AuthToken).where(
            AuthToken.token_hash == security.hash_token(token),
            AuthToken.kind == kind,
        )
    )
    rec = res.scalar_one_or_none()
    if rec is None or rec.used or rec.expires_at <= _now():
        raise AuthError(400, "Invalid or expired token")
    rec.used = True
    return rec


# ── login history ─────────────────────────────────────────────────


async def _record_login(
    db: AsyncSession,
    user: Optional[AuthUser],
    email: str,
    ip: Optional[str],
    device: Optional[str],
    *,
    success: bool,
    detail: Optional[str],
) -> None:
    db.add(
        LoginHistory(
            user_id=user.id if user else None,
            email=email,
            ip=ip,
            device=(device or "")[:256] or None,
            success=success,
            detail=detail,
        )
    )
