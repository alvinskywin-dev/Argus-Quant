"""
Sprint 21B — reconciliation API.

Mounted only when RECONCILIATION_ENABLED=true. Detection is read-only.
  GET  /api/reconciliation/status  — public aggregate (flag + counts, no PII)
  GET  /api/reconciliation/issues  — authed; the caller's own issues
  POST /api/reconciliation/run     — authed; reconcile the caller's accounts
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.auth.deps import get_current_user
from app.config import settings
from app.database.models import AuthUser
from app.database.session import get_session
from app.exchange_adapters import live_gate_open
from app.reconciliation import engine, report

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


@router.get("/status")
async def status():
    async with get_session() as db:
        agg = await report.summary(db)
    return {
        "enabled": settings.reconciliation_enabled,
        "mode": "LIVE" if live_gate_open() else "MOCK",
        **agg,
    }


@router.get("/issues")
async def issues(resolved: str = Query(default=""), limit: int = Query(default=200, le=1000),
                 user: AuthUser = Depends(get_current_user)):
    resolved_filter = None if resolved == "" else resolved.lower() in ("1", "true", "yes")
    async with get_session() as db:
        return await report.list_issues(db, user_id=user.id, resolved=resolved_filter, limit=limit)


@router.post("/run")
async def run(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        return await engine.reconcile_user(db, user_id=user.id)
