"""
Sprint 20F — live trading orchestration (Binance USDT-M Futures).

Resolves an adapter from the user's vaulted credentials (Sprint 20C), applies
the safety gate (Sprint 20E), executes via the unified adapter, and records
every order / fill / error / rejection to the live_* tables + audit log.

SAFETY: a real order is only ever placed when resolve_adapter returns a LIVE
adapter, which requires LIVE_TRADING_ENABLED=true AND MOCK_EXCHANGE_MODE=false.
By default everything runs MOCK and the result rows are tagged mode="MOCK".
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LiveAuditLog, LiveOrder, LivePosition, LiveTrade
from app.exchange_adapters import live_gate_open, resolve_adapter
from app.exchange_adapters.base import AdapterError, opposite_side, to_side
from app.exchange_vault import service as vault
from app.paper_engine import math as pmath
from app.safety import service as safety
from app.utils.logger import logger


class LiveTradingError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def gate_status() -> dict:
    from app.config import settings
    return {
        "live_trading_enabled": settings.live_trading_enabled,
        "mock_exchange_mode": settings.mock_exchange_mode,
        "live_gate_open": live_gate_open(),
        "mode": "LIVE" if live_gate_open() else "MOCK",
    }


async def _audit(db, user_id, exchange, symbol, action, result, mode, detail=""):
    """Audit in an independent session so failure records survive a rollback."""
    from app.database.session import SessionLocal
    try:
        async with SessionLocal() as adb:
            adb.add(LiveAuditLog(
                user_id=user_id, exchange=exchange, symbol=symbol or "", action=action,
                result=result, mode=mode, detail=(detail or "")[:512]))
            await adb.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[live] audit write failed: {exc}")


async def _adapter_for(db: AsyncSession, user_id: int, exchange: str):
    creds = await vault.get_decrypted_credentials(db, user_id, exchange)  # raises VaultError if none
    return resolve_adapter(
        exchange, api_key=creds["api_key"], api_secret=creds["api_secret"],
        passphrase=creds.get("passphrase"))


# ── open ──────────────────────────────────────────────────────────

async def open_position(
    db: AsyncSession, *, user_id: int, exchange: str, symbol: str, side: str,
    quantity: Optional[float] = None, notional_usdt: Optional[float] = None,
    entry_price: Optional[float] = None, leverage: int = 5, margin_type: str = "isolated",
    order_type: str = "MARKET", take_profit: Optional[float] = None,
    stop_loss: Optional[float] = None, trailing_pct: Optional[float] = None,
) -> dict:
    exchange = exchange.lower()
    symbol = symbol.upper()
    mode = "LIVE" if live_gate_open() else "MOCK"

    # Safety gate (global/user kill, lockout).
    blocked = await safety.trading_blocked(db, user_id)
    if blocked:
        await _audit(db, user_id, exchange, symbol, "OPEN", "REJECTED", mode, blocked)
        raise LiveTradingError(403, f"Blocked by safety: {blocked}")

    try:
        adapter = await _adapter_for(db, user_id, exchange)
    except vault.VaultError as exc:
        await _audit(db, user_id, exchange, symbol, "OPEN", "REJECTED", mode, exc.detail)
        raise LiveTradingError(exc.status_code, exc.detail)

    # Resolve quantity.
    px = float(entry_price or pmath_mark(symbol))
    if quantity is None:
        if not notional_usdt or px <= 0:
            raise LiveTradingError(400, "Provide quantity, or notional_usdt with a price")
        quantity = pmath.position_quantity(notional_usdt, px)
    if quantity <= 0:
        raise LiveTradingError(400, "Quantity must be positive")

    bside = to_side(side)
    try:
        await adapter.set_margin_type(symbol, margin_type)
        await adapter.set_leverage(symbol, leverage)
        order = await adapter.open_order(
            symbol=symbol, side=bside, qty=quantity, order_type=order_type, price=entry_price)
        if take_profit or stop_loss or trailing_pct:
            await adapter.set_tp_sl(
                symbol=symbol, side=bside, qty=quantity,
                take_profit=take_profit, stop_loss=stop_loss, trailing_pct=trailing_pct)
    except AdapterError as exc:
        await _record_order_error(db, user_id, exchange, symbol, bside, order_type, mode, str(exc))
        await _audit(db, user_id, exchange, symbol, "ERROR", "FAIL", mode, str(exc))
        raise LiveTradingError(502, f"Exchange error: {exc}")
    finally:
        await adapter.close()

    fill_price = order.avg_price or order.price or px
    db.add(LiveOrder(
        user_id=user_id, exchange=exchange, exchange_order_id=order.order_id, symbol=symbol,
        side=bside, order_type=order_type, price=fill_price, quantity=quantity,
        filled_qty=order.filled_qty, avg_price=order.avg_price, reduce_only=False,
        status=order.status, mode=order.mode))

    pos = LivePosition(
        user_id=user_id, exchange=exchange, symbol=symbol, side=side.upper(),
        quantity=quantity, entry_price=fill_price, leverage=leverage,
        margin_type=margin_type, status="OPEN", mode=order.mode)
    db.add(pos)
    await db.flush()
    await _audit(db, user_id, exchange, symbol, "OPEN", "OK", order.mode,
                 f"{side} {quantity}@{fill_price} lev={leverage} {order.mode}")
    logger.info(f"[live] OPEN user={user_id} {exchange} {symbol} {side} qty={quantity} mode={order.mode}")
    return {"order": _order_dict(order), "position_id": pos.id, "mode": order.mode}


# ── close ─────────────────────────────────────────────────────────

async def close_position(
    db: AsyncSession, *, user_id: int, position_id: int, exit_price: Optional[float] = None,
) -> dict:
    pos = await db.get(LivePosition, position_id)
    if pos is None or pos.user_id != user_id:
        raise LiveTradingError(404, "Position not found")
    if pos.status != "OPEN":
        raise LiveTradingError(409, "Position already closed")

    mode = pos.mode
    try:
        adapter = await _adapter_for(db, user_id, pos.exchange)
    except vault.VaultError as exc:
        raise LiveTradingError(exc.status_code, exc.detail)

    try:
        order = await adapter.close_order(symbol=pos.symbol, side=to_side(pos.side), qty=pos.quantity)
    except AdapterError as exc:
        await _audit(db, user_id, pos.exchange, pos.symbol, "ERROR", "FAIL", mode, str(exc))
        raise LiveTradingError(502, f"Exchange error: {exc}")
    finally:
        await adapter.close()

    exit_px = float(exit_price or order.avg_price or order.price or pmath_mark(pos.symbol) or pos.entry_price)
    notional = pos.quantity * pos.entry_price
    pnl = pmath.unrealized_pnl(pos.side, pos.entry_price, exit_px, notional)

    pos.status = "CLOSED"
    pos.realized_pnl = round(pnl, 6)
    pos.closed_at = _now()
    db.add(LiveOrder(
        user_id=user_id, exchange=pos.exchange, exchange_order_id=order.order_id, symbol=pos.symbol,
        side=opposite_side(to_side(pos.side)), order_type="MARKET", price=exit_px,
        quantity=pos.quantity, filled_qty=order.filled_qty, avg_price=order.avg_price,
        reduce_only=True, status=order.status, mode=order.mode))
    db.add(LiveTrade(
        user_id=user_id, position_id=pos.id, exchange=pos.exchange, symbol=pos.symbol,
        side=pos.side, entry_price=pos.entry_price, exit_price=exit_px, quantity=pos.quantity,
        leverage=pos.leverage, pnl_usdt=round(pnl, 6), mode=order.mode, opened_at=pos.opened_at))
    await db.flush()
    await _audit(db, user_id, pos.exchange, pos.symbol, "CLOSE", "OK", order.mode,
                 f"exit={exit_px} pnl={pnl:.2f} {order.mode}")
    logger.info(f"[live] CLOSE user={user_id} {pos.exchange} {pos.symbol} pnl={pnl:.2f} mode={order.mode}")
    return {"position_id": pos.id, "exit_price": exit_px, "pnl_usdt": round(pnl, 2), "mode": order.mode}


async def set_leverage(db: AsyncSession, *, user_id: int, exchange: str, symbol: str, leverage: int) -> dict:
    try:
        adapter = await _adapter_for(db, user_id, exchange.lower())
    except vault.VaultError as exc:
        raise LiveTradingError(exc.status_code, exc.detail)
    try:
        await adapter.set_leverage(symbol.upper(), leverage)
    except AdapterError as exc:
        raise LiveTradingError(502, f"Exchange error: {exc}")
    finally:
        await adapter.close()
    mode = "LIVE" if live_gate_open() else "MOCK"
    await _audit(db, user_id, exchange, symbol, "LEVERAGE", "OK", mode, f"leverage={leverage}")
    return {"symbol": symbol.upper(), "leverage": leverage, "mode": mode}


async def get_balance(db: AsyncSession, *, user_id: int, exchange: str) -> dict:
    try:
        adapter = await _adapter_for(db, user_id, exchange.lower())
    except vault.VaultError as exc:
        raise LiveTradingError(exc.status_code, exc.detail)
    try:
        bal = await adapter.get_balance()
    except AdapterError as exc:
        raise LiveTradingError(502, f"Exchange error: {exc}")
    finally:
        await adapter.close()
    return {"asset": bal.asset, "balance": bal.balance, "available": bal.available, "mode": bal.mode}


# ── listings ──────────────────────────────────────────────────────

async def list_positions(db, user_id, status: Optional[str] = None):
    q = select(LivePosition).where(LivePosition.user_id == user_id)
    if status:
        q = q.where(LivePosition.status == status.upper())
    q = q.order_by(LivePosition.opened_at.desc())
    return list((await db.execute(q)).scalars().all())


async def list_orders(db, user_id, limit: int = 200):
    return list((await db.execute(
        select(LiveOrder).where(LiveOrder.user_id == user_id)
        .order_by(LiveOrder.created_at.desc()).limit(limit))).scalars().all())


async def list_trades(db, user_id, limit: int = 200):
    return list((await db.execute(
        select(LiveTrade).where(LiveTrade.user_id == user_id)
        .order_by(LiveTrade.closed_at.desc()).limit(limit))).scalars().all())


# ── helpers ───────────────────────────────────────────────────────

def pmath_mark(symbol: str) -> float:
    try:
        from app.market_data.ws_engine import latest_prices
        return float(latest_prices.get(symbol, 0.0) or 0.0)
    except Exception:
        return 0.0


def _order_dict(order) -> dict:
    return {
        "order_id": order.order_id, "symbol": order.symbol, "side": order.side,
        "type": order.type, "status": order.status, "price": order.price,
        "qty": order.qty, "filled_qty": order.filled_qty, "mode": order.mode,
    }


async def _record_order_error(db, user_id, exchange, symbol, side, order_type, mode, err):
    from app.database.session import SessionLocal
    try:
        async with SessionLocal() as adb:
            adb.add(LiveOrder(
                user_id=user_id, exchange=exchange, symbol=symbol, side=side,
                order_type=order_type, status="REJECTED", mode=mode, error=err[:256]))
            await adb.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[live] order-error record failed: {exc}")
