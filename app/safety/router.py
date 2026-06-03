"""
Sprint 20E — safety layer API.

Mounted only when SAFETY_LAYER_ENABLED=true. User routes manage personal limits
and a personal kill switch; admin routes (ADMIN role) drive the GLOBAL emergency
stop that halts auto-trading for everyone instantly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user, require_role
from app.database.models import AuthUser, SafetyConfig
from app.database.session import get_session
from app.paper_engine import service as paper
from app.safety import service
from app.safety.schemas import (
    MessageOut,
    SafetyConfigOut,
    SafetyConfigUpdateIn,
    SafetyStatusOut,
)

router = APIRouter(prefix="/api/safety", tags=["safety"])
admin_router = APIRouter(prefix="/api/admin/safety", tags=["safety-admin"])


def _config_out(cfg: SafetyConfig) -> SafetyConfigOut:
    return SafetyConfigOut(
        max_daily_loss_pct=cfg.max_daily_loss_pct,
        max_weekly_loss_pct=cfg.max_weekly_loss_pct,
        max_open_positions=cfg.max_open_positions,
        max_correlated_positions=cfg.max_correlated_positions,
        max_leverage=cfg.max_leverage,
        trade_cooldown_minutes=cfg.trade_cooldown_minutes,
        loss_streak_limit=cfg.loss_streak_limit,
        loss_streak_cooldown_hours=cfg.loss_streak_cooldown_hours,
    )


# ── user routes ───────────────────────────────────────────────────


@router.get("/config", response_model=SafetyConfigOut)
async def get_config(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        return _config_out(await service.get_or_create_config(db, user.id))


@router.put("/config", response_model=SafetyConfigOut)
async def put_config(body: SafetyConfigUpdateIn, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        cfg = await service.update_config(db, user.id, body.model_dump(exclude_none=True))
        return _config_out(cfg)


@router.get("/status", response_model=SafetyStatusOut)
async def get_status(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        acc = await paper.get_or_create_account(db, user.id)
        return SafetyStatusOut(**await service.status(db, user.id, acc))


@router.post("/kill", response_model=MessageOut)
async def kill(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        await service.set_user_kill(db, user.id, True)
    return MessageOut(detail="Kill switch enabled — auto-trading stopped")


@router.post("/resume", response_model=MessageOut)
async def resume(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        await service.resume_user(db, user.id)
    return MessageOut(detail="Trading re-enabled")


# ── admin routes (global emergency stop) ──────────────────────────


@admin_router.post("/kill-all", response_model=MessageOut)
async def kill_all(user: AuthUser = Depends(require_role("ADMIN"))):
    async with get_session() as db:
        await service.set_global_kill(db, True)
    return MessageOut(detail="GLOBAL emergency stop ACTIVE — all auto-trading halted")


@admin_router.post("/resume-all", response_model=MessageOut)
async def resume_all(user: AuthUser = Depends(require_role("ADMIN"))):
    async with get_session() as db:
        await service.set_global_kill(db, False)
    return MessageOut(detail="Global emergency stop cleared")


@admin_router.get("/state", response_model=MessageOut)
async def global_state(user: AuthUser = Depends(require_role("ADMIN"))):
    async with get_session() as db:
        on = await service.get_global_kill(db)
    return MessageOut(detail=f"global_kill={'ON' if on else 'OFF'}")
