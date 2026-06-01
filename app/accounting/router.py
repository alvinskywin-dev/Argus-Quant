"""
Sprint 21E — net-PnL accounting API.

Mounted only when ACCOUNTING_ENABLED=true.
  GET /api/accounting/summary      — public aggregate (net PnL by mode, no PII)
  GET /api/accounting/daily        — authed; caller's per-day PnL
  GET /api/accounting/trades       — authed; caller's per-trade breakdowns
  GET /api/accounting/user/{id}    — authed; own summary (or admin for any user)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.accounting import service
from app.auth.deps import get_current_user
from app.config import settings
from app.database.models import AuthUser
from app.database.session import get_session

router = APIRouter(prefix="/api/accounting", tags=["accounting"])


@router.get("/summary")
async def summary():
    async with get_session() as db:
        agg = await service.summary(db)
    return {"enabled": settings.accounting_enabled, **agg}


@router.get("/daily")
async def daily(limit: int = 90, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        return await service.daily(db, user_id=user.id, limit=min(limit, 366))


@router.get("/trades")
async def trades(limit: int = 200, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        return await service.list_trades(db, user_id=user.id, limit=min(limit, 1000))


@router.get("/user/{user_id}")
async def user(user_id: int, user: AuthUser = Depends(get_current_user)):
    if user_id != user.id and getattr(user, "role", "") != "ADMIN":
        raise HTTPException(status_code=403, detail="Not allowed to view another user's accounting")
    async with get_session() as db:
        return await service.user_summary(db, user_id)
