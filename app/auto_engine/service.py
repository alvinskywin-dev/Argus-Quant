"""
Sprint 20D — auto-trade config + execution-log persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AutoTradeConfig, AutoTradeExecution


class AutoEngineError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_or_create_config(db: AsyncSession, user_id: int) -> AutoTradeConfig:
    res = await db.execute(select(AutoTradeConfig).where(AutoTradeConfig.user_id == user_id))
    cfg = res.scalar_one_or_none()
    if cfg is None:
        cfg = AutoTradeConfig(user_id=user_id)
        db.add(cfg)
        await db.flush()
    return cfg


_UPDATABLE = {
    "enabled",
    "live_enabled",
    "max_positions",
    "max_leverage",
    "risk_per_trade_pct",
    "allowed_exchanges",
    "allowed_coins",
    "min_confidence",
    "order_type",
    "use_break_even",
    "break_even_trigger",
    "use_trailing_stop",
    "trailing_distance_pct",
}


async def update_config(db: AsyncSession, user_id: int, changes: dict) -> AutoTradeConfig:
    cfg = await get_or_create_config(db, user_id)
    for key, value in changes.items():
        if value is not None and key in _UPDATABLE:
            setattr(cfg, key, value)
    return cfg


async def log_execution(
    db: AsyncSession,
    *,
    user_id: int,
    action: str,
    reason: str = "",
    signal_id: Optional[int] = None,
    account_id: Optional[int] = None,
    position_id: Optional[int] = None,
    symbol: str = "",
    detail: str = "",
) -> None:
    db.add(
        AutoTradeExecution(
            user_id=user_id,
            action=action,
            reason=reason[:64],
            signal_id=signal_id,
            account_id=account_id,
            position_id=position_id,
            symbol=symbol,
            detail=(detail or "")[:256],
        )
    )


async def list_executions(
    db: AsyncSession, user_id: int, limit: int = 100
) -> list[AutoTradeExecution]:
    res = await db.execute(
        select(AutoTradeExecution)
        .where(AutoTradeExecution.user_id == user_id)
        .order_by(AutoTradeExecution.created_at.desc())
        .limit(limit)
    )
    return list(res.scalars().all())


async def already_executed(db: AsyncSession, user_id: int, signal_id: int) -> bool:
    """True if this user already had an OPEN action for this signal (idempotency)."""
    res = await db.execute(
        select(AutoTradeExecution.id)
        .where(
            AutoTradeExecution.user_id == user_id,
            AutoTradeExecution.signal_id == signal_id,
            AutoTradeExecution.action == "OPEN",
        )
        .limit(1)
    )
    return res.first() is not None
