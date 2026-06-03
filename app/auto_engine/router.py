"""
Sprint 20D — auto-trading config/status API (DEMO).

Mounted only when AUTO_TRADE_DEMO_ENABLED=true. Requires a 20A token. The
engine itself runs server-side off signal/tracker events; these endpoints just
let a user configure and inspect their demo auto-trading.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user
from app.auto_engine import engine, service
from app.auto_engine.schemas import (
    AutoConfigOut,
    AutoConfigUpdateIn,
    ExecutionOut,
    StatusOut,
)
from app.database.models import AuthUser, AutoTradeConfig
from app.database.session import get_session

router = APIRouter(prefix="/api/auto", tags=["auto-trading"])


def _config_out(cfg: AutoTradeConfig) -> AutoConfigOut:
    return AutoConfigOut(
        enabled=cfg.enabled,
        max_positions=cfg.max_positions,
        max_leverage=cfg.max_leverage,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        allowed_exchanges=cfg.allowed_exchanges,
        allowed_coins=cfg.allowed_coins,
        min_confidence=cfg.min_confidence,
        order_type=cfg.order_type,
        use_break_even=cfg.use_break_even,
        break_even_trigger=cfg.break_even_trigger,
        use_trailing_stop=cfg.use_trailing_stop,
        trailing_distance_pct=cfg.trailing_distance_pct,
    )


@router.get("/config", response_model=AutoConfigOut)
async def get_config(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        cfg = await service.get_or_create_config(db, user.id)
        return _config_out(cfg)


@router.put("/config", response_model=AutoConfigOut)
async def put_config(body: AutoConfigUpdateIn, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        cfg = await service.update_config(db, user.id, body.model_dump(exclude_none=True))
        return _config_out(cfg)


@router.get("/status", response_model=StatusOut)
async def get_status(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        return StatusOut(**await engine.status(db, user.id))


@router.get("/executions", response_model=list[ExecutionOut])
async def get_executions(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        rows = await service.list_executions(db, user.id)
        return [
            ExecutionOut(
                id=e.id,
                signal_id=e.signal_id,
                account_id=e.account_id,
                position_id=e.position_id,
                symbol=e.symbol,
                action=e.action,
                reason=e.reason,
                detail=e.detail,
                created_at=e.created_at,
            )
            for e in rows
        ]
