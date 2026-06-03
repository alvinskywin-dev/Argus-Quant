"""
Sprint 21C — position recovery API.

Mounted only when POSITION_RECOVERY_ENABLED=true.
  GET  /api/recovery/status — public aggregate (flag + unsafe/recovered counts)
  POST /api/recovery/run    — authed; recover the caller's positions
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user
from app.config import settings
from app.database.models import AuthUser
from app.database.session import get_session
from app.recovery import engine
from app.recovery import status as rstatus

router = APIRouter(prefix="/api/recovery", tags=["recovery"])


@router.get("/status")
async def status():
    async with get_session() as db:
        agg = await rstatus.recovery_status(db)
    return {"enabled": settings.position_recovery_enabled, **agg}


@router.post("/run")
async def run(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        return await engine.recover_user_positions(db, user_id=user.id)
