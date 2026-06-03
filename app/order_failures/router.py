"""
Sprint 21D — order failure / retry API.

Mounted only when ORDER_FAILURE_ENGINE_ENABLED=true.
  GET  /api/order-failures                — public aggregate summary (no PII)
  GET  /api/order-failures/list           — authed; the caller's failures
  GET  /api/order-failures/{id}           — authed; one failure (owner only)
  POST /api/order-failures/{id}/retry     — authed; recompute the retry decision
  POST /api/order-failures/{id}/mark-resolved — authed; close it out

NOTE: /retry recomputes and persists the retry DECISION; it does not itself
place an order (the execution layer acts on the decision). This keeps the API
incapable of triggering real orders directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import get_current_user
from app.config import settings
from app.database.models import AuthUser
from app.database.session import get_session
from app.order_failures import service

router = APIRouter(prefix="/api/order-failures", tags=["order-failures"])


@router.get("")
async def overview():
    async with get_session() as db:
        agg = await service.summary(db)
    return {"enabled": settings.order_failure_engine_enabled, **agg}


@router.get("/list")
async def list_own(
    final_state: str = "", limit: int = 200, user: AuthUser = Depends(get_current_user)
):
    async with get_session() as db:
        return await service.list_failures(
            db, user_id=user.id, final_state=final_state or None, limit=min(limit, 1000)
        )


@router.get("/{failure_id}")
async def detail(failure_id: int, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        row = await service.get_failure(db, failure_id)
    if row is None or row["user_id"] != user.id:
        raise HTTPException(status_code=404, detail="Order failure not found")
    return row


@router.post("/{failure_id}/retry")
async def retry(failure_id: int, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        row = await service.get_failure(db, failure_id)
        if row is None or row["user_id"] != user.id:
            raise HTTPException(status_code=404, detail="Order failure not found")
        return await service.note_retry(db, failure_id)


@router.post("/{failure_id}/mark-resolved")
async def mark_resolved(failure_id: int, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        row = await service.get_failure(db, failure_id)
        if row is None or row["user_id"] != user.id:
            raise HTTPException(status_code=404, detail="Order failure not found")
        return await service.mark_resolved(db, failure_id)
