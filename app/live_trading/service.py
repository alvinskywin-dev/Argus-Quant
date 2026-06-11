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

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LiveAuditLog, LiveOrder, LivePosition, LiveTrade
from app.exchange_adapters import live_gate_open, resolve_adapter
from app.exchange_adapters.base import AdapterError, AdapterTimeoutError, opposite_side, to_side
from app.exchange_vault import service as vault
from app.paper_engine import math as pmath
from app.recovery import tp_sl as tp_sl_const
from app.safety import service as safety
from app.utils.logger import logger


class LiveTradingError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Binance newClientOrderId allows ^[\.A-Z\:/a-z0-9_-]{1,36}$.
_CID_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.:/")


def _build_client_order_id(supplied: Optional[str]) -> str:
    """Return a valid idempotency key — caller-supplied (sanitised) or generated."""
    if supplied:
        cid = "".join(c for c in str(supplied) if c in _CID_ALLOWED)[:36]
        if cid:
            return cid
    return f"ax{uuid.uuid4().hex}"[:36]


async def _resolve_after_timeout(
    adapter, symbol: str, client_order_id: str, *, attempts: int = 3, delay: float = 0.5
):
    """After an ambiguous timeout, ask the exchange whether the order landed.

    Returns the OrderResult if found, else None. Retries because a just-placed
    order can take a moment to become queryable.
    """
    for i in range(attempts):
        try:
            res = await adapter.get_order_by_client_id(
                symbol=symbol, client_order_id=client_order_id
            )
        except AdapterError:
            res = None
        if res is not None:
            return res
        if i < attempts - 1:
            await asyncio.sleep(delay)
    return None


async def _open_entry_idempotent(adapter, *, symbol, side, qty, order_type, price, client_order_id):
    """Place the entry order; on a timeout, resolve the true state by client id.

    This is the heart of the duplicate-safety fix: a dropped connection no longer
    means "assume it failed and risk a second fill" — we look the order up by its
    idempotency key and only report failure when the exchange truly has no order.
    """
    try:
        return await adapter.open_order(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            price=price,
            client_order_id=client_order_id,
        )
    except AdapterTimeoutError as exc:
        logger.warning(
            f"[live] entry timeout for {symbol} {side} — resolving by "
            f"client_order_id={client_order_id}"
        )
        resolved = await _resolve_after_timeout(adapter, symbol, client_order_id)
        if resolved is not None:
            logger.warning(
                f"[live] entry DID land despite timeout {symbol} {side} "
                f"order_id={resolved.order_id} — adopting it (no duplicate placed)"
            )
            return resolved
        # The exchange has no such order — safe to treat as a clean failure.
        raise AdapterError(
            f"entry timed out and no order found for client_order_id={client_order_id}: {exc}"
        ) from exc


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
            adb.add(
                LiveAuditLog(
                    user_id=user_id,
                    exchange=exchange,
                    symbol=symbol or "",
                    action=action,
                    result=result,
                    mode=mode,
                    detail=(detail or "")[:512],
                )
            )
            await adb.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[live] audit write failed: {exc}")


async def _adapter_for(db: AsyncSession, user_id: int, exchange: str):
    creds = await vault.get_decrypted_credentials(
        db, user_id, exchange
    )  # raises VaultError if none
    return resolve_adapter(
        exchange,
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        passphrase=creds.get("passphrase"),
    )


# ── auto-routing (Signal → Exchange Adapter → Execution) ──────────


async def connected_exchanges(db: AsyncSession, user_id: int) -> list[str]:
    """Exchanges the user has a CONNECTED vault account for."""
    return [a.exchange for a in await vault.list_accounts(db, user_id) if a.status == "CONNECTED"]


async def route_exchange(db: AsyncSession, user_id: int, preferred: Optional[str] = None) -> str:
    """Pick the exchange to route an order to: preferred if connected, else the first connected."""
    connected = await connected_exchanges(db, user_id)
    if not connected:
        raise LiveTradingError(
            400, "No connected exchange to route to. Connect one in the vault first."
        )
    if preferred and preferred.lower() in connected:
        return preferred.lower()
    return connected[0]


# ── open ──────────────────────────────────────────────────────────


async def open_position(
    db: AsyncSession,
    *,
    user_id: int,
    exchange: str,
    symbol: str,
    side: str,
    quantity: Optional[float] = None,
    notional_usdt: Optional[float] = None,
    entry_price: Optional[float] = None,
    leverage: int = 5,
    margin_type: str = "isolated",
    order_type: str = "MARKET",
    take_profit: Optional[float] = None,
    stop_loss: Optional[float] = None,
    trailing_pct: Optional[float] = None,
    client_order_id: Optional[str] = None,
) -> dict:
    exchange = exchange.lower()
    if exchange == "auto":
        exchange = await route_exchange(db, user_id)  # Signal -> Exchange Adapter routing
    symbol = symbol.upper()
    mode = "LIVE" if live_gate_open() else "MOCK"

    # Safety gate (global/user kill, lockout).
    blocked = await safety.trading_blocked(db, user_id)
    if blocked:
        await _audit(db, user_id, exchange, symbol, "OPEN", "REJECTED", mode, blocked)
        raise LiveTradingError(403, f"Blocked by safety: {blocked}")

    # Multi-user Live Beta gate (no-op unless LIVE_BETA_ENABLED). Enforces the
    # per-user / per-symbol / global exposure envelope for beta members.
    from app.live_beta import service as live_beta

    if live_beta.beta_enabled():
        est_notional = float(notional_usdt or (quantity or 0) * (entry_price or pmath_mark(symbol)))
        beta_block = await live_beta.beta_gate(
            db, user_id=user_id, exchange=exchange, symbol=symbol, notional_usdt=est_notional
        )
        if beta_block:
            await _audit(db, user_id, exchange, symbol, "OPEN", "REJECTED", mode, beta_block)
            raise LiveTradingError(403, f"Blocked by live beta: {beta_block}")

    try:
        adapter = await _adapter_for(db, user_id, exchange)
    except vault.VaultError as exc:
        await _audit(db, user_id, exchange, symbol, "OPEN", "REJECTED", mode, exc.detail)
        raise LiveTradingError(exc.status_code, exc.detail) from exc

    # Resolve quantity.
    px = float(entry_price or pmath_mark(symbol))
    if quantity is None:
        if not notional_usdt or px <= 0:
            raise LiveTradingError(400, "Provide quantity, or notional_usdt with a price")
        quantity = pmath.position_quantity(notional_usdt, px)
    if quantity <= 0:
        raise LiveTradingError(400, "Quantity must be positive")

    bside = to_side(side)
    cid = _build_client_order_id(client_order_id)

    # ── 1) ENTRY ─────────────────────────────────────────────────────
    # If the entry itself fails, no position exists — record + reject cleanly.
    # The order carries an idempotency key (cid); a dropped connection is
    # resolved by looking the order up rather than risking a duplicate fill.
    try:
        await adapter.set_margin_type(symbol, margin_type)
        await adapter.set_leverage(symbol, leverage)
        order = await _open_entry_idempotent(
            adapter,
            symbol=symbol,
            side=bside,
            qty=quantity,
            order_type=order_type,
            price=entry_price,
            client_order_id=cid,
        )
    except AdapterError as exc:
        await _record_order_error(db, user_id, exchange, symbol, bside, order_type, mode, str(exc))
        await _record_failure(
            db,
            user_id,
            exchange,
            symbol,
            bside,
            order_type,
            mode,
            quantity,
            entry_price or 0.0,
            exc,
            is_tp_sl=False,
        )
        await _audit(db, user_id, exchange, symbol, "ERROR", "FAIL", mode, str(exc))
        await adapter.close()
        raise LiveTradingError(502, f"Exchange error: {exc}") from exc

    # Entry filled — persist the order + position FIRST so a real fill is never
    # lost even if protection placement fails below.
    fill_price = order.avg_price or order.price or px
    db.add(
        LiveOrder(
            user_id=user_id,
            exchange=exchange,
            exchange_order_id=order.order_id,
            symbol=symbol,
            side=bside,
            order_type=order_type,
            price=fill_price,
            quantity=quantity,
            filled_qty=order.filled_qty,
            avg_price=order.avg_price,
            reduce_only=False,
            status=order.status,
            mode=order.mode,
        )
    )

    # ── 2) PROTECTION (TP/SL) — partial-failure aware ────────────────
    want_protection = bool(take_profit or stop_loss or trailing_pct)
    tp_sl_status = tp_sl_const.SYNCED  # nothing-to-protect counts as synced
    unsafe_reason: Optional[str] = None
    if want_protection:
        try:
            await adapter.set_tp_sl(
                symbol=symbol,
                side=bside,
                qty=quantity,
                take_profit=take_profit,
                stop_loss=stop_loss,
                trailing_pct=trailing_pct,
            )
            tp_sl_status = tp_sl_const.SYNCED
        except AdapterError as exc:
            # CRITICAL partial failure: entry is LIVE but UNPROTECTED. Keep the
            # position, mark it UNSAFE for the recovery engine to retry / the
            # admin to emergency-close. Do NOT raise — losing track is worse.
            tp_sl_status = tp_sl_const.UNSAFE
            unsafe_reason = f"TP/SL placement failed after entry: {exc}"[:256]
            await _record_failure(
                db,
                user_id,
                exchange,
                symbol,
                bside,
                "TP_SL",
                order.mode,
                quantity,
                0.0,
                exc,
                is_tp_sl=True,
            )
            await _audit(db, user_id, exchange, symbol, "TP_SL", "FAIL", order.mode, unsafe_reason)
            logger.warning(
                f"[live] UNPROTECTED position user={user_id} {exchange} {symbol}: {unsafe_reason}"
            )
    await adapter.close()

    pos = LivePosition(
        user_id=user_id,
        exchange=exchange,
        symbol=symbol,
        side=side.upper(),
        quantity=quantity,
        entry_price=fill_price,
        leverage=leverage,
        margin_type=margin_type,
        status="OPEN",
        mode=order.mode,
        take_profit=take_profit,
        stop_loss=stop_loss,
        tp_sl_status=tp_sl_status,
        unsafe_reason=unsafe_reason,
        requires_review=(tp_sl_status == tp_sl_const.UNSAFE),
    )
    db.add(pos)
    await db.flush()
    await _audit(
        db,
        user_id,
        exchange,
        symbol,
        "OPEN",
        "OK",
        order.mode,
        f"{side} {quantity}@{fill_price} lev={leverage} {order.mode} tp_sl={tp_sl_status}",
    )
    logger.info(
        f"[live] OPEN user={user_id} {exchange} {symbol} {side} qty={quantity} "
        f"mode={order.mode} tp_sl={tp_sl_status}"
    )
    return {
        "order": _order_dict(order),
        "position_id": pos.id,
        "mode": order.mode,
        "tp_sl_status": tp_sl_status,
        "unsafe": tp_sl_status == tp_sl_const.UNSAFE,
    }


# ── close ─────────────────────────────────────────────────────────


async def close_position(
    db: AsyncSession,
    *,
    user_id: int,
    position_id: int,
    exit_price: Optional[float] = None,
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
        raise LiveTradingError(exc.status_code, exc.detail) from exc

    try:
        order = await adapter.close_order(
            symbol=pos.symbol, side=to_side(pos.side), qty=pos.quantity
        )
    except AdapterError as exc:
        await _audit(db, user_id, pos.exchange, pos.symbol, "ERROR", "FAIL", mode, str(exc))
        raise LiveTradingError(502, f"Exchange error: {exc}") from exc
    finally:
        await adapter.close()

    exit_px = float(
        exit_price or order.avg_price or order.price or pmath_mark(pos.symbol) or pos.entry_price
    )
    notional = pos.quantity * pos.entry_price
    pnl = pmath.unrealized_pnl(pos.side, pos.entry_price, exit_px, notional)

    pos.status = "CLOSED"
    pos.realized_pnl = round(pnl, 6)
    pos.closed_at = _now()
    db.add(
        LiveOrder(
            user_id=user_id,
            exchange=pos.exchange,
            exchange_order_id=order.order_id,
            symbol=pos.symbol,
            side=opposite_side(to_side(pos.side)),
            order_type="MARKET",
            price=exit_px,
            quantity=pos.quantity,
            filled_qty=order.filled_qty,
            avg_price=order.avg_price,
            reduce_only=True,
            status=order.status,
            mode=order.mode,
        )
    )
    trade = LiveTrade(
        user_id=user_id,
        position_id=pos.id,
        exchange=pos.exchange,
        symbol=pos.symbol,
        side=pos.side,
        entry_price=pos.entry_price,
        exit_price=exit_px,
        quantity=pos.quantity,
        leverage=pos.leverage,
        pnl_usdt=round(pnl, 6),
        mode=order.mode,
        opened_at=pos.opened_at,
    )
    db.add(trade)
    await db.flush()
    await _record_accounting(
        db,
        trade=trade,
        gross_pnl=pnl,
        entry_price=pos.entry_price,
        exit_price=exit_px,
        expected_exit=exit_price,
        opened_at=pos.opened_at,
        closed_at=pos.closed_at,
    )
    await _audit(
        db,
        user_id,
        pos.exchange,
        pos.symbol,
        "CLOSE",
        "OK",
        order.mode,
        f"exit={exit_px} pnl={pnl:.2f} {order.mode}",
    )
    logger.info(
        f"[live] CLOSE user={user_id} {pos.exchange} {pos.symbol} pnl={pnl:.2f} mode={order.mode}"
    )
    return {
        "position_id": pos.id,
        "exit_price": exit_px,
        "pnl_usdt": round(pnl, 2),
        "mode": order.mode,
    }


EMERGENCY_CONFIRM_PHRASE = "CLOSE UNSAFE POSITION"


async def emergency_close_position(
    db: AsyncSession,
    *,
    position_id: int,
    reason: str,
    actor_user_id: int,
    is_admin: bool = False,
) -> dict:
    """
    Force-close a (possibly UNSAFE / RECOVERED) position with a REDUCE-ONLY
    market order. Steps, each audited:
      1) authorise (owner or admin) + require EMERGENCY_CLOSE_ENABLED,
      2) reconcile: read the true exchange position first,
      3) cancel stale TP/SL working orders,
      4) reduce-only market close (never opens an opposite position),
      5) finalise DB state + accounting + alert admin.
    """
    from app.config import settings

    if not settings.emergency_close_enabled:
        raise LiveTradingError(403, "Emergency close is disabled (EMERGENCY_CLOSE_ENABLED=false).")

    pos = await db.get(LivePosition, position_id)
    if pos is None:
        raise LiveTradingError(404, "Position not found")
    if pos.user_id != actor_user_id and not is_admin:
        raise LiveTradingError(403, "Not allowed to close another user's position")
    if pos.status in ("CLOSED", "CLOSED_UNKNOWN"):
        raise LiveTradingError(409, "Position already closed")

    mode = pos.mode
    await _audit(
        db,
        pos.user_id,
        pos.exchange,
        pos.symbol,
        "EMERGENCY_CLOSE",
        "START",
        mode,
        f"reason={reason} actor={actor_user_id} admin={is_admin}",
    )

    try:
        adapter = await _adapter_for(db, pos.user_id, pos.exchange)
    except vault.VaultError as exc:
        raise LiveTradingError(exc.status_code, exc.detail) from exc

    try:
        # 2) Reconcile against the exchange — close the ACTUAL size if known.
        ex_qty = pos.quantity
        try:
            ex_positions = await adapter.get_positions()
            match = next((p for p in ex_positions if p.symbol == pos.symbol), None)
            if match is not None and match.qty > 0:
                ex_qty = match.qty
            elif match is None:
                # Already flat on the exchange — nothing to close.
                pos.status = "CLOSED_UNKNOWN"
                pos.closed_at = _now()
                pos.tp_sl_status = "UNKNOWN"
                pos.unsafe_reason = (reason or "")[:256]
                await db.flush()
                await _audit(
                    db,
                    pos.user_id,
                    pos.exchange,
                    pos.symbol,
                    "EMERGENCY_CLOSE",
                    "OK",
                    mode,
                    "position already flat on exchange -> CLOSED_UNKNOWN",
                )
                return {
                    "position_id": pos.id,
                    "closed": False,
                    "status": "CLOSED_UNKNOWN",
                    "mode": mode,
                    "note": "exchange already flat",
                }
        except AdapterError as exc:
            await _audit(
                db,
                pos.user_id,
                pos.exchange,
                pos.symbol,
                "EMERGENCY_CLOSE",
                "FAIL",
                mode,
                f"reconcile read failed: {exc}",
            )

        # 3) Cancel stale TP/SL (best-effort; never raises the close).
        try:
            await adapter.cancel_all_orders(pos.symbol)
            await _audit(
                db,
                pos.user_id,
                pos.exchange,
                pos.symbol,
                "EMERGENCY_CLOSE",
                "OK",
                mode,
                "cancelled working orders",
            )
        except AdapterError as exc:
            await _audit(
                db,
                pos.user_id,
                pos.exchange,
                pos.symbol,
                "EMERGENCY_CLOSE",
                "FAIL",
                mode,
                f"cancel orders failed: {exc}",
            )

        # 4) Reduce-only market close.
        try:
            order = await adapter.close_order(symbol=pos.symbol, side=to_side(pos.side), qty=ex_qty)
        except AdapterError as exc:
            await mark_position_unsafe_fallback(db, pos, f"emergency close failed: {exc}")
            await _audit(
                db, pos.user_id, pos.exchange, pos.symbol, "EMERGENCY_CLOSE", "FAIL", mode, str(exc)
            )
            raise LiveTradingError(502, f"Emergency close failed: {exc}") from exc
    finally:
        await adapter.close()

    # 5) Finalise.
    exit_px = float(order.avg_price or order.price or pmath_mark(pos.symbol) or pos.entry_price)
    notional = ex_qty * pos.entry_price
    pnl = pmath.unrealized_pnl(pos.side, pos.entry_price, exit_px, notional)
    pos.status = "CLOSED"
    pos.realized_pnl = round(pnl, 6)
    pos.closed_at = _now()
    pos.tp_sl_status = "SYNCED"
    pos.unsafe_reason = None
    db.add(
        LiveOrder(
            user_id=pos.user_id,
            exchange=pos.exchange,
            exchange_order_id=order.order_id,
            symbol=pos.symbol,
            side=opposite_side(to_side(pos.side)),
            order_type="MARKET",
            price=exit_px,
            quantity=ex_qty,
            filled_qty=order.filled_qty,
            avg_price=order.avg_price,
            reduce_only=True,
            status=order.status,
            mode=order.mode,
        )
    )
    trade = LiveTrade(
        user_id=pos.user_id,
        position_id=pos.id,
        exchange=pos.exchange,
        symbol=pos.symbol,
        side=pos.side,
        entry_price=pos.entry_price,
        exit_price=exit_px,
        quantity=ex_qty,
        leverage=pos.leverage,
        pnl_usdt=round(pnl, 6),
        mode=order.mode,
        opened_at=pos.opened_at,
    )
    db.add(trade)
    await db.flush()
    await _record_accounting(
        db,
        trade=trade,
        gross_pnl=pnl,
        entry_price=pos.entry_price,
        exit_price=exit_px,
        opened_at=pos.opened_at,
        closed_at=pos.closed_at,
    )
    await _audit(
        db,
        pos.user_id,
        pos.exchange,
        pos.symbol,
        "EMERGENCY_CLOSE",
        "OK",
        order.mode,
        f"reduce-only close qty={ex_qty} exit={exit_px} pnl={pnl:.2f} reason={reason}",
    )
    logger.warning(
        f"[live] EMERGENCY_CLOSE user={pos.user_id} {pos.exchange} {pos.symbol} "
        f"pnl={pnl:.2f} mode={order.mode} reason={reason}"
    )
    return {
        "position_id": pos.id,
        "closed": True,
        "exit_price": exit_px,
        "pnl_usdt": round(pnl, 2),
        "mode": order.mode,
    }


async def mark_position_unsafe_fallback(db, pos, reason: str) -> None:
    """Mark a position UNSAFE in-line (used when an emergency close itself fails)."""
    pos.tp_sl_status = "UNSAFE"
    pos.unsafe_reason = (reason or "")[:256]
    pos.requires_review = True
    await db.flush()


async def set_leverage(
    db: AsyncSession, *, user_id: int, exchange: str, symbol: str, leverage: int
) -> dict:
    try:
        adapter = await _adapter_for(db, user_id, exchange.lower())
    except vault.VaultError as exc:
        raise LiveTradingError(exc.status_code, exc.detail) from exc
    try:
        await adapter.set_leverage(symbol.upper(), leverage)
    except AdapterError as exc:
        raise LiveTradingError(502, f"Exchange error: {exc}") from exc
    finally:
        await adapter.close()
    mode = "LIVE" if live_gate_open() else "MOCK"
    await _audit(db, user_id, exchange, symbol, "LEVERAGE", "OK", mode, f"leverage={leverage}")
    return {"symbol": symbol.upper(), "leverage": leverage, "mode": mode}


async def get_balance(db: AsyncSession, *, user_id: int, exchange: str) -> dict:
    try:
        adapter = await _adapter_for(db, user_id, exchange.lower())
    except vault.VaultError as exc:
        raise LiveTradingError(exc.status_code, exc.detail) from exc
    try:
        bal = await adapter.get_balance()
    except AdapterError as exc:
        raise LiveTradingError(502, f"Exchange error: {exc}") from exc
    finally:
        await adapter.close()
    return {
        "asset": bal.asset,
        "balance": bal.balance,
        "available": bal.available,
        "mode": bal.mode,
    }


# ── Sprint 21F — read-only Binance preflight ────────────────────────


async def binance_preflight(
    db: AsyncSession,
    *,
    user_id: int,
    testnet: Optional[bool] = None,
    symbol: str = "BTCUSDT",
) -> dict:
    """
    Run the read-only Binance preflight with the user's vaulted key.

    Places no orders — only signed reads + public data. Used to prove a key works
    end-to-end (auth, clock, symbol filters, positions) before a real test. Honors
    the ``binance_testnet`` setting unless ``testnet`` is given explicitly.
    """
    from app.config import settings
    from app.exchange_vault.binance_preflight import run_binance_preflight

    if testnet is None:
        testnet = settings.binance_testnet
    try:
        creds = await vault.get_decrypted_credentials(db, user_id, "binance")
    except vault.VaultError as exc:
        raise LiveTradingError(exc.status_code, exc.detail) from exc
    result = await run_binance_preflight(
        creds["api_key"], creds["api_secret"], testnet=bool(testnet), symbol=symbol
    )
    await _audit(
        db,
        user_id,
        "binance",
        symbol,
        "preflight",
        "OK" if result.ok else "FAIL",
        "LIVE" if not testnet else "MOCK",
        detail=f"testnet={testnet} ok={result.ok}",
    )
    return result.to_public_dict()


# ── listings ──────────────────────────────────────────────────────


async def list_positions(db, user_id, status: Optional[str] = None):
    q = select(LivePosition).where(LivePosition.user_id == user_id)
    if status:
        q = q.where(LivePosition.status == status.upper())
    q = q.order_by(LivePosition.opened_at.desc())
    return list((await db.execute(q)).scalars().all())


async def list_orders(db, user_id, limit: int = 200):
    return list(
        (
            await db.execute(
                select(LiveOrder)
                .where(LiveOrder.user_id == user_id)
                .order_by(LiveOrder.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def list_trades(db, user_id, limit: int = 200):
    return list(
        (
            await db.execute(
                select(LiveTrade)
                .where(LiveTrade.user_id == user_id)
                .order_by(LiveTrade.closed_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


# ── helpers ───────────────────────────────────────────────────────


def pmath_mark(symbol: str) -> float:
    try:
        from app.market_data.ws_engine import latest_prices

        return float(latest_prices.get(symbol, 0.0) or 0.0)
    except Exception:
        return 0.0


def _order_dict(order) -> dict:
    return {
        "order_id": order.order_id,
        "symbol": order.symbol,
        "side": order.side,
        "type": order.type,
        "status": order.status,
        "price": order.price,
        "qty": order.qty,
        "filled_qty": order.filled_qty,
        "mode": order.mode,
    }


async def _record_order_error(db, user_id, exchange, symbol, side, order_type, mode, err):
    from app.database.session import SessionLocal

    try:
        async with SessionLocal() as adb:
            adb.add(
                LiveOrder(
                    user_id=user_id,
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    status="REJECTED",
                    mode=mode,
                    error=err[:256],
                )
            )
            await adb.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[live] order-error record failed: {exc}")


async def _record_accounting(
    db,
    *,
    trade,
    gross_pnl,
    entry_price,
    exit_price,
    expected_exit=None,
    opened_at=None,
    closed_at=None,
) -> None:
    """
    Sprint 21E — record the net-PnL breakdown for a closed trade. Uses the
    caller's session (same transaction as the close). No-op unless enabled and
    never breaks the close on error.
    """
    from app.config import settings

    if not settings.accounting_enabled:
        return
    try:
        from app.accounting import service as acct

        await acct.record_trade_accounting(
            db,
            user_id=trade.user_id,
            exchange=trade.exchange,
            symbol=trade.symbol,
            side=trade.side,
            mode=trade.mode,
            gross_pnl=gross_pnl,
            quantity=trade.quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            leverage=trade.leverage,
            trade_id=trade.id,
            expected_exit=expected_exit,
            opened_at=opened_at,
            closed_at=closed_at,
        )
    except Exception as e:  # noqa: BLE001 — accounting must never break the close
        logger.warning(f"[live] accounting record skipped: {e}")


async def _record_failure(
    db,
    user_id,
    exchange,
    symbol,
    side,
    order_type,
    mode,
    qty,
    price,
    exc: Exception,
    *,
    is_tp_sl: bool = False,
) -> None:
    """
    Sprint 21D — classify + persist the failure via the order-failure engine
    (own session so it survives the caller's rollback). No-op unless enabled.
    """
    from app.config import settings

    if not settings.order_failure_engine_enabled:
        return
    from app.database.session import SessionLocal
    from app.order_failures import service as of_service

    try:
        async with SessionLocal() as adb:
            await of_service.record_failure(
                adb,
                user_id=user_id,
                exchange=exchange,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=qty,
                price=price,
                reduce_only=is_tp_sl,
                error_message=str(exc),
                is_tp_sl=is_tp_sl,
                mode=mode,
            )
            await adb.commit()
    except Exception as e:  # noqa: BLE001 — failure recording must never break the flow
        logger.warning(f"[live] failure record skipped: {e}")
