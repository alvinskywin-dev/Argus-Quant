"""
Sprint 21E — net-PnL accounting orchestration (DB layer).

Records a per-trade net-PnL breakdown when a position closes, writes the raw
fee/funding events, and rolls the result into the per-user/day aggregate.
Commission/slippage/funding are estimated when the exchange has not supplied
settled values, in which case the breakdown is flagged PARTIAL/ESTIMATED.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounting import pnl as pnlmath
from app.accounting.models import (
    DailyUserPnl,
    ExchangeFeeEvent,
    FundingFeeEvent,
    LiveTradeAccounting,
)
from app.utils.logger import logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def record_trade_accounting(
    db: AsyncSession, *, user_id: int, exchange: str, symbol: str, side: str, mode: str,
    gross_pnl: float, quantity: float, entry_price: float, exit_price: float,
    leverage: int = 1, trade_id: Optional[int] = None,
    commission: Optional[float] = None, funding_fee: Optional[float] = None,
    slippage: Optional[float] = None, expected_exit: Optional[float] = None,
    opened_at: Optional[datetime] = None, closed_at: Optional[datetime] = None,
) -> dict:
    """Compute + persist the net-PnL breakdown for one closed trade."""
    notional = abs(quantity * entry_price)
    margin = (notional / leverage) if leverage else notional

    commission_known = commission is not None
    funding_known = funding_fee is not None
    slippage_known = slippage is not None

    if commission is None:
        commission = pnlmath.estimate_commission(notional)
    if funding_fee is None:
        funding_fee = 0.0
    if slippage is None:
        slippage = (pnlmath.slippage_cost(expected_exit, exit_price, quantity)
                    if expected_exit else 0.0)

    bd = pnlmath.compute_net_pnl(
        gross_pnl, commission=commission, funding_fee=funding_fee, slippage=slippage,
        margin=margin, commission_known=commission_known,
        funding_known=funding_known, slippage_known=slippage_known)

    closed_at = closed_at or _now()
    row = LiveTradeAccounting(
        trade_id=trade_id, user_id=user_id, exchange=exchange, symbol=symbol, side=side,
        mode=mode, gross_pnl=bd.gross_pnl, commission=bd.commission, funding_fee=bd.funding_fee,
        slippage=bd.slippage, net_pnl=bd.net_pnl, net_roe=bd.net_roe, realized_pnl=gross_pnl,
        total_fees=bd.total_fees, estimate_quality=bd.estimate_quality,
        opened_at=opened_at, closed_at=closed_at,
        holding_time_seconds=pnlmath.holding_seconds(opened_at, closed_at))
    db.add(row)

    # Raw, append-only fee/funding events (never overwritten).
    db.add(ExchangeFeeEvent(
        user_id=user_id, exchange=exchange, symbol=symbol, trade_id=trade_id,
        fee_type="COMMISSION", amount=bd.commission, mode=mode, estimated=not commission_known))
    if bd.slippage:
        db.add(ExchangeFeeEvent(
            user_id=user_id, exchange=exchange, symbol=symbol, trade_id=trade_id,
            fee_type="SLIPPAGE", amount=bd.slippage, mode=mode, estimated=not slippage_known))
    if funding_fee:
        db.add(FundingFeeEvent(
            user_id=user_id, exchange=exchange, symbol=symbol, amount=funding_fee, mode=mode))

    await _roll_daily(db, user_id=user_id, mode=mode, breakdown=bd, when=closed_at)
    await db.flush()
    logger.info(f"[accounting] user={user_id} {exchange} {symbol} net={bd.net_pnl} "
                f"({bd.estimate_quality}) mode={mode}")
    return {
        "trade_id": trade_id, "net_pnl": bd.net_pnl, "gross_pnl": bd.gross_pnl,
        "commission": bd.commission, "funding_fee": bd.funding_fee, "slippage": bd.slippage,
        "net_roe": bd.net_roe, "estimate_quality": bd.estimate_quality, "mode": mode,
    }


async def _roll_daily(db: AsyncSession, *, user_id: int, mode: str,
                      breakdown: pnlmath.PnlBreakdown, when: datetime) -> None:
    day = when.date()
    row = (await db.execute(
        select(DailyUserPnl).where(
            DailyUserPnl.user_id == user_id, DailyUserPnl.day == day,
            DailyUserPnl.mode == mode))).scalar_one_or_none()
    if row is None:
        row = DailyUserPnl(user_id=user_id, day=day, mode=mode)
        db.add(row)
    row.gross_pnl = (row.gross_pnl or 0.0) + breakdown.gross_pnl
    row.net_pnl = (row.net_pnl or 0.0) + breakdown.net_pnl
    row.commission = (row.commission or 0.0) + breakdown.commission
    row.funding_fee = (row.funding_fee or 0.0) + breakdown.funding_fee
    row.slippage = (row.slippage or 0.0) + breakdown.slippage
    row.trades_count = (row.trades_count or 0) + 1
    if breakdown.net_pnl >= 0:
        row.wins = (row.wins or 0) + 1
    else:
        row.losses = (row.losses or 0) + 1


# ── read APIs ───────────────────────────────────────────────────────

async def summary(db: AsyncSession, *, user_id: Optional[int] = None) -> dict:
    q = select(
        LiveTradeAccounting.mode,
        func.count(), func.coalesce(func.sum(LiveTradeAccounting.gross_pnl), 0.0),
        func.coalesce(func.sum(LiveTradeAccounting.net_pnl), 0.0),
        func.coalesce(func.sum(LiveTradeAccounting.total_fees), 0.0),
    ).group_by(LiveTradeAccounting.mode)
    if user_id is not None:
        q = q.where(LiveTradeAccounting.user_id == user_id)
    by_mode: dict[str, dict] = {}
    for mode, n, gross, net, fees in (await db.execute(q)).all():
        by_mode[mode] = {
            "trades": int(n), "gross_pnl": round(float(gross), 4),
            "net_pnl": round(float(net), 4), "total_fees": round(float(fees), 4)}
    return {
        "by_mode": by_mode,
        "net_pnl_live": by_mode.get("LIVE", {}).get("net_pnl", 0.0),
        "net_pnl_mock": by_mode.get("MOCK", {}).get("net_pnl", 0.0),
    }


def _trade_dict(t: LiveTradeAccounting) -> dict:
    return {
        "id": t.id, "trade_id": t.trade_id, "user_id": t.user_id, "exchange": t.exchange,
        "symbol": t.symbol, "side": t.side, "mode": t.mode, "gross_pnl": t.gross_pnl,
        "commission": t.commission, "funding_fee": t.funding_fee, "slippage": t.slippage,
        "net_pnl": t.net_pnl, "net_roe": t.net_roe, "total_fees": t.total_fees,
        "estimate_quality": t.estimate_quality, "holding_time_seconds": t.holding_time_seconds,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


async def list_trades(db: AsyncSession, *, user_id: Optional[int] = None, limit: int = 200) -> list[dict]:
    q = select(LiveTradeAccounting)
    if user_id is not None:
        q = q.where(LiveTradeAccounting.user_id == user_id)
    q = q.order_by(LiveTradeAccounting.created_at.desc()).limit(limit)
    return [_trade_dict(t) for t in (await db.execute(q)).scalars().all()]


async def daily(db: AsyncSession, *, user_id: Optional[int] = None, limit: int = 90) -> list[dict]:
    q = select(DailyUserPnl)
    if user_id is not None:
        q = q.where(DailyUserPnl.user_id == user_id)
    q = q.order_by(DailyUserPnl.day.desc()).limit(limit)
    out = []
    for d in (await db.execute(q)).scalars().all():
        out.append({
            "day": d.day.isoformat(), "mode": d.mode, "gross_pnl": round(d.gross_pnl, 4),
            "net_pnl": round(d.net_pnl, 4), "commission": round(d.commission, 4),
            "funding_fee": round(d.funding_fee, 4), "slippage": round(d.slippage, 4),
            "trades_count": d.trades_count, "wins": d.wins, "losses": d.losses})
    return out


async def user_summary(db: AsyncSession, user_id: int) -> dict:
    return {"user_id": user_id, **(await summary(db, user_id=user_id)),
            "recent_trades": await list_trades(db, user_id=user_id, limit=50)}
