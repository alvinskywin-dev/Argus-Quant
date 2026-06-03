"""
Sprint 20A — auth API router.

Mounted only when AUTH_ENABLED=true (see app/dashboard/server.py:create_app).
All persistence goes through app.auth.service; this layer only handles HTTP.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.auth import service
from app.auth.deps import (
    client_device,
    client_ip,
    get_current_user,
)
from app.auth.schemas import (
    Enable2FAVerifyIn,
    ForgotPasswordIn,
    LoginIn,
    LogoutIn,
    MessageOut,
    RefreshIn,
    RegisterIn,
    ResetPasswordIn,
    SessionOut,
    TokenOut,
    TwoFactorSetupOut,
    UpdateTimezoneIn,
    UserOut,
    VerifyEmailIn,
)
from app.config import settings
from app.database.models import AuthUser
from app.database.session import get_session
from app.utils.timezone import SUPPORTED_TIMEZONES, is_supported_timezone

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _err(exc: service.AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _user_out(user: AuthUser) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        username=user.username,
        role=user.role,
        status=user.status,
        is_verified=user.is_verified,
        totp_enabled=user.totp_enabled,
        telegram_user_id=user.telegram_user_id,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        timezone=getattr(user, "timezone", None) or "UTC",
    )


def _tokens(access: str, refresh: str) -> TokenOut:
    return TokenOut(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_min * 60,
    )


# ── registration / login ──────────────────────────────────────────

@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: RegisterIn, request: Request):
    try:
        async with get_session() as db:
            user = await service.register(
                db, email=body.email, password=body.password, username=body.username
            )
            return _user_out(user)
    except service.AuthError as exc:
        return _err(exc)


@router.post("/login")
async def login(body: LoginIn, request: Request):
    try:
        async with get_session() as db:
            user, access, refresh = await service.authenticate(
                db,
                email=body.email,
                password=body.password,
                totp_code=body.totp_code,
                ip=client_ip(request),
                device=client_device(request),
            )
            return _tokens(access, refresh)
    except service.AuthError as exc:
        return _err(exc)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, request: Request):
    try:
        async with get_session() as db:
            user, access = await service.refresh_access_token(
                db,
                refresh_token=body.refresh_token,
                ip=client_ip(request),
                device=client_device(request),
            )
            # Refresh token is unchanged (sliding access tokens, fixed session).
            return _tokens(access, body.refresh_token)
    except service.AuthError as exc:
        return _err(exc)


@router.post("/logout", response_model=MessageOut)
async def logout(body: LogoutIn):
    async with get_session() as db:
        await service.logout(db, refresh_token=body.refresh_token)
    return MessageOut(detail="Logged out")


@router.get("/me", response_model=UserOut)
async def me(user: AuthUser = Depends(get_current_user)):
    return _user_out(user)


@router.get("/timezones", response_model=dict)
async def timezones():
    """Supported display timezones (public — used to populate the picker)."""
    return {"timezones": SUPPORTED_TIMEZONES, "default": "UTC"}


@router.put("/timezone", response_model=UserOut)
async def update_timezone(body: UpdateTimezoneIn, user: AuthUser = Depends(get_current_user)):
    """Set the current user's display timezone. Rejects unsupported zones (400)."""
    tz = (body.timezone or "").strip()
    if not is_supported_timezone(tz):
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported timezone. Allowed: {', '.join(SUPPORTED_TIMEZONES)}"})
    async with get_session() as db:
        db_user = await db.get(AuthUser, user.id)
        if db_user is None:
            return JSONResponse(status_code=404, content={"detail": "User not found"})
        db_user.timezone = tz
        await db.commit()
        await db.refresh(db_user)
        return _user_out(db_user)


# ── email verification ────────────────────────────────────────────

@router.post("/verify-email", response_model=MessageOut)
async def verify_email_post(body: VerifyEmailIn):
    try:
        async with get_session() as db:
            await service.verify_email(db, token=body.token)
        return MessageOut(detail="Email verified")
    except service.AuthError as exc:
        return _err(exc)


@router.get("/verify-email", response_model=MessageOut)
async def verify_email_get(token: str = Query(...)):
    try:
        async with get_session() as db:
            await service.verify_email(db, token=token)
        return MessageOut(detail="Email verified")
    except service.AuthError as exc:
        return _err(exc)


# ── password reset ────────────────────────────────────────────────

@router.post("/forgot-password", response_model=MessageOut)
async def forgot_password(body: ForgotPasswordIn):
    async with get_session() as db:
        await service.request_password_reset(db, email=body.email)
    # Always 200 — never reveal whether the email exists.
    return MessageOut(detail="If that email exists, a reset link has been sent")


@router.post("/reset-password", response_model=MessageOut)
async def reset_password(body: ResetPasswordIn):
    try:
        async with get_session() as db:
            await service.reset_password(db, token=body.token, new_password=body.new_password)
        return MessageOut(detail="Password updated")
    except service.AuthError as exc:
        return _err(exc)


# ── 2FA (TOTP) ────────────────────────────────────────────────────

@router.post("/2fa/setup", response_model=TwoFactorSetupOut)
async def setup_2fa(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        managed = await db.get(AuthUser, user.id)
        secret, uri = await service.begin_2fa_setup(db, managed)
    return TwoFactorSetupOut(secret=secret, otpauth_uri=uri)


@router.post("/2fa/enable", response_model=MessageOut)
async def enable_2fa(body: Enable2FAVerifyIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            managed = await db.get(AuthUser, user.id)
            await service.confirm_2fa(db, managed, body.code)
        return MessageOut(detail="2FA enabled")
    except service.AuthError as exc:
        return _err(exc)


@router.post("/2fa/disable", response_model=MessageOut)
async def disable_2fa(body: Enable2FAVerifyIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            managed = await db.get(AuthUser, user.id)
            await service.disable_2fa(db, managed, body.code)
        return MessageOut(detail="2FA disabled")
    except service.AuthError as exc:
        return _err(exc)


# ── sessions / login history ──────────────────────────────────────

@router.get("/sessions", response_model=list[SessionOut])
async def sessions(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        rows = await service.list_sessions(db, user.id)
        return [
            SessionOut(
                id=s.id,
                ip=s.ip,
                device=s.device,
                created_at=s.created_at,
                last_seen=s.last_seen,
                expires_at=s.expires_at,
            )
            for s in rows
        ]
