"""
Sprint 20C — exchange vault API.

Mounted only when EXCHANGE_API_VAULT_ENABLED=true. All routes require an
authenticated user (Sprint 20A). Responses NEVER include decrypted secrets.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth.deps import client_ip, get_current_user
from app.database.models import AuthUser, ExchangeAccount
from app.database.session import get_session
from app.exchange_vault import service
from app.exchange_vault.schemas import (
    AccountRef,
    ConnectIn,
    ExchangeAccountOut,
    MessageOut,
    TestResultOut,
)

router = APIRouter(prefix="/api/exchange", tags=["exchange-vault"])


def _err(exc: service.VaultError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _account_out(a: ExchangeAccount) -> ExchangeAccountOut:
    return ExchangeAccountOut(
        id=a.id,
        exchange=a.exchange,
        label=a.label,
        status=a.status,
        api_key_last4=a.api_key_last4,
        can_read=a.can_read,
        can_trade=a.can_trade,
        can_futures=a.can_futures,
        can_withdraw=a.can_withdraw,
        last_validation_status=a.last_validation_status,
        permission_warning=a.permission_warning,
        last_error=a.last_error,
        last_test=a.last_test,
        created_at=a.created_at,
    )


@router.post("/connect", response_model=ExchangeAccountOut, status_code=201)
async def connect(body: ConnectIn, request: Request, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            acc = await service.connect(
                db,
                user_id=user.id,
                exchange=body.exchange,
                api_key=body.api_key,
                api_secret=body.api_secret,
                passphrase=body.passphrase,
                label=body.label,
                ip=client_ip(request),
            )
            return _account_out(acc)
    except service.VaultError as exc:
        return _err(exc)


@router.post("/test", response_model=TestResultOut)
async def test(body: AccountRef, request: Request, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            acc, result = await service.test_connection(
                db, user_id=user.id, exchange=body.exchange,
                label=body.label, ip=client_ip(request),
            )
            return TestResultOut(
                exchange=acc.exchange,
                label=acc.label,
                status=acc.status,
                last_validation_status=result.status,
                can_read=result.can_read,
                can_trade=result.can_trade,
                can_futures=result.can_futures,
                can_withdraw=result.can_withdraw,
                permission_warning=result.permission_warning or None,
                error_code=result.error_code or None,
                message=result.permission_warning or result.error_message or result.status,
            )
    except service.VaultError as exc:
        return _err(exc)


@router.post("/disconnect", response_model=MessageOut)
async def disconnect(body: AccountRef, request: Request, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            await service.disconnect(
                db, user_id=user.id, exchange=body.exchange,
                label=body.label, ip=client_ip(request),
            )
        return MessageOut(detail=f"{body.exchange}/{body.label} disconnected")
    except service.VaultError as exc:
        return _err(exc)


@router.get("/accounts", response_model=list[ExchangeAccountOut])
async def accounts(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        rows = await service.list_accounts(db, user.id)
        return [_account_out(a) for a in rows]
