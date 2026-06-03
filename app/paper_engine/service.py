"""
Sprint 20B — paper-trading business logic.

All functions take an AsyncSession and operate on the paper_* per-user tables.
They raise PaperError (mapped to HTTP status codes by the router) on any
expected failure. Pure maths lives in app.paper_engine.math.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    PaperAccount,
    PaperAccountPosition,
    PaperOrder,
    PaperTrade,
    Signal,
)
from app.paper_engine import math as pmath
from app.utils.logger import logger

OPEN_STATUS = "OPEN"


class PaperError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mark_price(symbol: str) -> float:
    """
    Latest realtime mark price from the price cache, or 0.0 if unavailable.

    It NEVER falls back to entry_price — doing so would make every position
    read 0% ROE / $0 PnL while looking "live". Callers must treat 0.0 as
    "no mark yet" (display "—", contribute nothing to PnL) and should call
    `ensure_marks(...)` first so the cache is populated.
    """
    try:
        from app.market_data.ws_engine import latest_prices

        return float(latest_prices.get(symbol, 0.0) or 0.0)
    except Exception:
        return 0.0


def mark_price_info(symbol: str) -> tuple[float, str, Optional[float]]:
    """(price, source, age_seconds) for diagnostics. source ∈ live|none."""
    try:
        from app.market_data import ws_engine

        px = float(ws_engine.latest_prices.get(symbol, 0.0) or 0.0)
        age = ws_engine.price_age(symbol)
        return (px, "live" if px > 0 else "none", age)
    except Exception:
        return (0.0, "none", None)


async def ensure_marks(symbols) -> None:
    """Populate the price cache with fresh live marks for `symbols`."""
    try:
        from app.market_data.ws_engine import ensure_prices

        await ensure_prices(symbols)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[paper] ensure_marks skipped: {exc}")


# ── account lifecycle ─────────────────────────────────────────────


async def get_or_create_account(db: AsyncSession, user_id: int) -> PaperAccount:
    res = await db.execute(select(PaperAccount).where(PaperAccount.user_id == user_id))
    acc = res.scalar_one_or_none()
    if acc is None:
        bal = settings.default_demo_balance
        acc = PaperAccount(
            user_id=user_id,
            initial_balance=bal,
            balance=bal,
            default_leverage=10,
        )
        db.add(acc)
        await db.flush()
        logger.info(f"[paper] created account id={acc.id} user={user_id} balance={bal}")
    return acc


async def reset_account(db: AsyncSession, account: PaperAccount) -> None:
    for model in (PaperTrade, PaperOrder, PaperAccountPosition):
        rows = await db.execute(select(model).where(model.account_id == account.id))
        for row in rows.scalars().all():
            await db.delete(row)
    account.balance = account.initial_balance
    account.auto_follow = False


async def set_auto_follow(db: AsyncSession, account: PaperAccount, enabled: bool) -> None:
    account.auto_follow = enabled


# ── open ──────────────────────────────────────────────────────────


async def _open_positions(db: AsyncSession, account_id: int) -> list[PaperAccountPosition]:
    res = await db.execute(
        select(PaperAccountPosition).where(
            PaperAccountPosition.account_id == account_id,
            PaperAccountPosition.status == OPEN_STATUS,
        )
    )
    return list(res.scalars().all())


async def _used_margin(db: AsyncSession, account_id: int) -> float:
    res = await db.execute(
        select(sqlfunc.coalesce(sqlfunc.sum(PaperAccountPosition.margin_usdt), 0.0)).where(
            PaperAccountPosition.account_id == account_id,
            PaperAccountPosition.status == OPEN_STATUS,
        )
    )
    return float(res.scalar_one() or 0.0)


async def open_position(
    db: AsyncSession,
    account: PaperAccount,
    *,
    symbol: str,
    side: str,
    entry_price: float,
    leverage: int,
    margin_usdt: Optional[float] = None,
    notional_usdt: Optional[float] = None,
    stop_loss: Optional[float] = None,
    tp1: Optional[float] = None,
    tp2: Optional[float] = None,
    tp3: Optional[float] = None,
    order_type: str = "MARKET",
    signal_id: Optional[int] = None,
) -> PaperAccountPosition:
    if entry_price <= 0:
        raise PaperError(400, "entry_price must be positive")
    leverage = max(1, int(leverage))

    if notional_usdt is None:
        if margin_usdt is None:
            raise PaperError(400, "Provide either margin_usdt or notional_usdt")
        notional_usdt = margin_usdt * leverage
    notional_usdt = float(notional_usdt)
    if notional_usdt <= 0:
        raise PaperError(400, "Position notional must be positive")

    margin = pmath.required_margin(notional_usdt, leverage)
    used = await _used_margin(db, account.id)
    available = account.balance - used
    if margin > available + 1e-9:
        raise PaperError(
            400,
            f"Insufficient available margin: need {margin:.2f}, have {available:.2f}",
        )

    qty = pmath.position_quantity(notional_usdt, entry_price)
    liq = pmath.liquidation_price(side, entry_price, leverage)

    order = PaperOrder(
        account_id=account.id,
        symbol=symbol,
        side=side,
        order_type=order_type,
        price=entry_price,
        quantity=qty,
        notional_usdt=notional_usdt,
        reduce_only=False,
        status="FILLED" if order_type == "MARKET" else "NEW",
        filled_at=_now() if order_type == "MARKET" else None,
    )
    db.add(order)

    pos = PaperAccountPosition(
        account_id=account.id,
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        quantity=qty,
        notional_usdt=notional_usdt,
        leverage=leverage,
        margin_usdt=margin,
        liquidation_price=liq,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        status=OPEN_STATUS,
    )
    db.add(pos)
    await db.flush()
    order.position_id = pos.id
    # Start polling this symbol's live price so its mark/ROE/PnL updates.
    try:
        from app.market_data.ws_engine import register_symbols

        register_symbols([symbol])
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        f"[paper] open acc={account.id} {side} {symbol} "
        f"notional={notional_usdt:.2f} lev={leverage}x margin={margin:.2f}"
    )
    return pos


async def copy_signal(
    db: AsyncSession, account: PaperAccount, signal: Signal, leverage: Optional[int] = None
) -> PaperAccountPosition:
    entry = float(signal.entry_low or 0)
    if entry <= 0:
        raise PaperError(400, "Signal has no usable entry price")
    lev = int(leverage or account.default_leverage)
    notional = pmath.risk_based_notional(
        account.balance,
        settings.paper_risk_per_trade_pct,
        entry,
        float(signal.stop_loss or 0),
        leverage=lev,
        max_notional_frac=0.5,
    )
    if notional <= 0:
        raise PaperError(400, "Computed position size is zero")
    return await open_position(
        db,
        account,
        symbol=signal.symbol,
        side=signal.side,
        entry_price=entry,
        leverage=lev,
        notional_usdt=notional,
        stop_loss=float(signal.stop_loss or 0) or None,
        tp1=float(signal.tp1 or 0) or None,
        tp2=float(signal.tp2 or 0) or None,
        tp3=float(signal.tp3 or 0) or None,
        signal_id=signal.id,
    )


def simulate_signal(account: PaperAccount, signal: Signal, leverage: Optional[int] = None) -> dict:
    """Dry-run projection (no DB writes) of opening a position from a signal."""
    entry = float(signal.entry_low or 0)
    if entry <= 0:
        raise PaperError(400, "Signal has no usable entry price")
    lev = int(leverage or account.default_leverage)
    notional = pmath.risk_based_notional(
        account.balance,
        settings.paper_risk_per_trade_pct,
        entry,
        float(signal.stop_loss or 0),
        leverage=lev,
        max_notional_frac=0.5,
    )
    margin = pmath.required_margin(notional, lev)
    qty = pmath.position_quantity(notional, entry)
    projections: dict[str, dict] = {}
    targets = {
        "TP1": float(signal.tp1 or 0),
        "TP2": float(signal.tp2 or 0),
        "TP3": float(signal.tp3 or 0),
        "SL": float(signal.stop_loss or 0),
    }
    for name, price in targets.items():
        if price <= 0:
            continue
        pnl = pmath.unrealized_pnl(signal.side, entry, price, notional)
        projections[name] = {
            "price": round(price, 8),
            "pnl_usdt": round(pnl, 2),
            "roe_pct": round(pmath.roe_pct(pnl, margin), 2),
        }
    return {
        "symbol": signal.symbol,
        "side": signal.side,
        "entry_price": round(entry, 8),
        "leverage": lev,
        "notional_usdt": round(notional, 2),
        "margin_usdt": round(margin, 2),
        "quantity": round(qty, 8),
        "liquidation_price": round(pmath.liquidation_price(signal.side, entry, lev), 8),
        "projections": projections,
    }


# ── close ─────────────────────────────────────────────────────────


async def close_position(
    db: AsyncSession,
    account: PaperAccount,
    position_id: int,
    *,
    mark: Optional[float] = None,
    reason: str = "MANUAL",
    funding_rate: float = 0.0,
    intervals: int = 0,
) -> PaperTrade:
    res = await db.execute(
        select(PaperAccountPosition).where(
            PaperAccountPosition.id == position_id,
            PaperAccountPosition.account_id == account.id,
        )
    )
    pos = res.scalar_one_or_none()
    if pos is None:
        raise PaperError(404, "Position not found")
    if pos.status != OPEN_STATUS:
        raise PaperError(409, "Position already closed")

    if mark and mark > 0:
        exit_price = float(mark)
    else:
        # Pull a fresh live mark before closing; only as an absolute last resort
        # (price feed unreachable) do we settle at entry to avoid a phantom PnL.
        await ensure_marks([pos.symbol])
        exit_price = mark_price(pos.symbol) or float(pos.entry_price)
    gross = pmath.unrealized_pnl(pos.side, pos.entry_price, exit_price, pos.notional_usdt)
    funding = pmath.funding_cost(pos.side, pos.notional_usdt, funding_rate, intervals)
    realized = gross - funding

    if pmath.is_liquidated(pos.side, exit_price, pos.liquidation_price):
        reason = "LIQUIDATION"
        # On liquidation the trader loses the full margin, no more.
        realized = -pos.margin_usdt

    pos.status = "LIQUIDATED" if reason == "LIQUIDATION" else "CLOSED"
    pos.realized_pnl_usdt = round(realized, 6)
    pos.funding_usdt = round(funding, 6)
    pos.closed_at = _now()

    account.balance = round(account.balance + realized, 6)

    db.add(
        PaperOrder(
            account_id=account.id,
            position_id=pos.id,
            symbol=pos.symbol,
            side="SHORT" if pos.side == "LONG" else "LONG",
            order_type="MARKET",
            price=exit_price,
            quantity=pos.quantity,
            notional_usdt=pos.notional_usdt,
            reduce_only=True,
            status="FILLED",
            filled_at=_now(),
        )
    )
    trade = PaperTrade(
        account_id=account.id,
        position_id=pos.id,
        signal_id=pos.signal_id,
        symbol=pos.symbol,
        side=pos.side,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=pos.quantity,
        notional_usdt=pos.notional_usdt,
        leverage=pos.leverage,
        pnl_usdt=round(realized, 6),
        pnl_pct=round(pmath.roe_pct(realized, pos.margin_usdt), 4),
        funding_usdt=round(funding, 6),
        reason=reason,
        opened_at=pos.opened_at,
    )
    db.add(trade)
    await db.flush()
    logger.info(
        f"[paper] close acc={account.id} pos={pos.id} {pos.symbol} "
        f"reason={reason} pnl={realized:.2f} balance={account.balance:.2f}"
    )
    return trade


async def check_liquidations(db: AsyncSession, account: PaperAccount) -> list[int]:
    """Close any open positions whose mark price has crossed liquidation."""
    closed: list[int] = []
    open_pos = await _open_positions(db, account.id)
    await ensure_marks([p.symbol for p in open_pos])
    for pos in open_pos:
        px = mark_price(pos.symbol)
        if px > 0 and pmath.is_liquidated(pos.side, px, pos.liquidation_price):
            await close_position(db, account, pos.id, mark=px, reason="LIQUIDATION")
            closed.append(pos.id)
    return closed


# ── queries / summary ─────────────────────────────────────────────


async def account_summary(db: AsyncSession, account: PaperAccount) -> dict:
    open_pos = await _open_positions(db, account.id)
    used_margin = sum(p.margin_usdt for p in open_pos)
    await ensure_marks([p.symbol for p in open_pos])
    unrealized = 0.0
    for p in open_pos:
        px = mark_price(p.symbol)
        if px > 0:  # no live mark yet → contribute nothing, never mark at entry
            unrealized += pmath.unrealized_pnl(p.side, p.entry_price, px, p.notional_usdt)

    trades = await list_trades(db, account.id, limit=100_000)
    wins = sum(1 for t in trades if t.pnl_usdt > 0)
    total_trades = len(trades)

    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_pnl = sum(t.pnl_usdt for t in trades if t.closed_at and t.closed_at >= midnight)

    realized = account.balance - account.initial_balance
    return {
        "account_id": account.id,
        "currency": account.currency,
        "initial_balance": round(account.initial_balance, 2),
        "balance": round(account.balance, 2),
        "used_margin": round(used_margin, 2),
        "available_balance": round(account.balance - used_margin, 2),
        "unrealized_pnl": round(unrealized, 2),
        "equity": round(account.balance + unrealized, 2),
        "open_positions": len(open_pos),
        "realized_pnl": round(realized, 2),
        "total_pnl": round(realized + unrealized, 2),
        "daily_pnl": round(daily_pnl, 2),
        "win_rate": round(wins / total_trades * 100, 1) if total_trades else 0.0,
        "total_trades": total_trades,
        "auto_follow": account.auto_follow,
        "default_leverage": account.default_leverage,
    }


async def list_positions(
    db: AsyncSession, account_id: int, status: Optional[str] = None, limit: int = 200
) -> list[PaperAccountPosition]:
    q = select(PaperAccountPosition).where(PaperAccountPosition.account_id == account_id)
    if status == "open":
        q = q.where(PaperAccountPosition.status == OPEN_STATUS)
    elif status == "closed":
        q = q.where(PaperAccountPosition.status != OPEN_STATUS)
    q = q.order_by(PaperAccountPosition.opened_at.desc()).limit(limit)
    res = await db.execute(q)
    return list(res.scalars().all())


async def list_orders(db: AsyncSession, account_id: int, limit: int = 200) -> list[PaperOrder]:
    res = await db.execute(
        select(PaperOrder)
        .where(PaperOrder.account_id == account_id)
        .order_by(PaperOrder.created_at.desc())
        .limit(limit)
    )
    return list(res.scalars().all())


async def list_trades(db: AsyncSession, account_id: int, limit: int = 200) -> list[PaperTrade]:
    res = await db.execute(
        select(PaperTrade)
        .where(PaperTrade.account_id == account_id)
        .order_by(PaperTrade.closed_at.desc())
        .limit(limit)
    )
    return list(res.scalars().all())
