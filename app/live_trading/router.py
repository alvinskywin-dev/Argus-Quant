"""
Sprint 20F — live-trading API (Binance USDT-M Futures).

Mounted only when LIVE_TRADING_ENABLED is configured to expose the API (the
flag also gates real execution). Requires a 20A token and a connected exchange
(20C). Every response carries `mode` = MOCK or LIVE; real orders happen only
when the live gate is open.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.auth.deps import get_current_user
from app.database.models import AuthUser, LiveOrder, LivePosition, LiveTrade
from app.database.session import get_session
from app.live_trading import service
from app.live_trading.schemas import (
    CloseLiveIn,
    EmergencyCloseIn,
    GateStatusOut,
    LiveOrderOut,
    LivePositionOut,
    LiveTradeOut,
    OpenLiveIn,
    SetLeverageIn,
)

router = APIRouter(prefix="/api/live", tags=["live-trading"])


def _err(exc: service.LiveTradingError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@router.get("/status", response_model=GateStatusOut)
async def status():
    return GateStatusOut(**service.gate_status())


@router.get("/exchanges", response_model=dict)
async def exchanges(user: AuthUser = Depends(get_current_user)):
    from app.exchange_adapters import SUPPORTED_EXCHANGES

    async with get_session() as db:
        connected = await service.connected_exchanges(db, user.id)
        routed = connected[0] if connected else None
        return {
            "supported": list(SUPPORTED_EXCHANGES),
            "connected": connected,
            "auto_routes_to": routed,
            **service.gate_status(),
        }


@router.get("/balance", response_model=dict)
async def balance(exchange: str = Query("binance"), user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            return await service.get_balance(db, user_id=user.id, exchange=exchange)
    except service.LiveTradingError as exc:
        return _err(exc)


@router.post("/open", response_model=dict, status_code=201)
async def open_position(body: OpenLiveIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            return await service.open_position(
                db,
                user_id=user.id,
                exchange=body.exchange,
                symbol=body.symbol,
                side=body.side,
                quantity=body.quantity,
                notional_usdt=body.notional_usdt,
                entry_price=body.entry_price,
                leverage=body.leverage,
                margin_type=body.margin_type,
                order_type=body.order_type,
                take_profit=body.take_profit,
                stop_loss=body.stop_loss,
                trailing_pct=body.trailing_pct,
            )
    except service.LiveTradingError as exc:
        return _err(exc)


@router.post("/close", response_model=dict)
async def close_position(body: CloseLiveIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            return await service.close_position(
                db, user_id=user.id, position_id=body.position_id, exit_price=body.exit_price
            )
    except service.LiveTradingError as exc:
        return _err(exc)


@router.post("/leverage", response_model=dict)
async def set_leverage(body: SetLeverageIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            return await service.set_leverage(
                db,
                user_id=user.id,
                exchange=body.exchange,
                symbol=body.symbol,
                leverage=body.leverage,
            )
    except service.LiveTradingError as exc:
        return _err(exc)


@router.post("/positions/{position_id}/emergency-close", response_model=dict)
async def emergency_close(
    position_id: int, body: EmergencyCloseIn, user: AuthUser = Depends(get_current_user)
):
    # Confirmation-phrase guard — a deliberate, hard-to-fat-finger action.
    if body.confirm.strip() != service.EMERGENCY_CONFIRM_PHRASE:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f'Confirmation required: type exactly "{service.EMERGENCY_CONFIRM_PHRASE}".'
            },
        )
    try:
        async with get_session() as db:
            return await service.emergency_close_position(
                db,
                position_id=position_id,
                reason=body.reason,
                actor_user_id=user.id,
                is_admin=(getattr(user, "role", "") == "ADMIN"),
            )
    except service.LiveTradingError as exc:
        return _err(exc)


@router.post("/binance/preflight", response_model=dict)
async def binance_preflight(
    symbol: str = Query("BTCUSDT"),
    testnet: bool | None = Query(default=None),
    user: AuthUser = Depends(get_current_user),
):
    """Read-only Binance preflight (Sprint 21F). Places no orders; flag-gated."""
    from app.config import settings

    if not settings.binance_preflight_enabled:
        return JSONResponse(status_code=404, content={"detail": "Binance preflight is disabled."})
    try:
        async with get_session() as db:
            return await service.binance_preflight(
                db, user_id=user.id, testnet=testnet, symbol=symbol
            )
    except service.LiveTradingError as exc:
        return _err(exc)


@router.get("/positions", response_model=list[LivePositionOut])
async def positions(status: str = Query(default=""), user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        rows = await service.list_positions(db, user.id, status=status or None)
        return [_pos_out(p) for p in rows]


@router.get("/orders", response_model=list[LiveOrderOut])
async def orders(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        rows = await service.list_orders(db, user.id)
        return [_order_out(o) for o in rows]


@router.get("/trades", response_model=list[LiveTradeOut])
async def trades(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        rows = await service.list_trades(db, user.id)
        return [_trade_out(t) for t in rows]


def _pos_out(p: LivePosition) -> LivePositionOut:
    return LivePositionOut(
        id=p.id,
        exchange=p.exchange,
        symbol=p.symbol,
        side=p.side,
        quantity=p.quantity,
        entry_price=p.entry_price,
        leverage=p.leverage,
        margin_type=p.margin_type,
        status=p.status,
        realized_pnl=round(p.realized_pnl, 2),
        mode=p.mode,
        tp_sl_status=getattr(p, "tp_sl_status", "UNKNOWN"),
        requires_review=getattr(p, "requires_review", False),
        unsafe_reason=getattr(p, "unsafe_reason", None),
        opened_at=p.opened_at,
        closed_at=p.closed_at,
    )


def _order_out(o: LiveOrder) -> LiveOrderOut:
    return LiveOrderOut(
        id=o.id,
        exchange=o.exchange,
        exchange_order_id=o.exchange_order_id,
        symbol=o.symbol,
        side=o.side,
        order_type=o.order_type,
        price=o.price,
        quantity=o.quantity,
        filled_qty=o.filled_qty,
        reduce_only=o.reduce_only,
        status=o.status,
        mode=o.mode,
        error=o.error,
        created_at=o.created_at,
    )


def _trade_out(t: LiveTrade) -> LiveTradeOut:
    return LiveTradeOut(
        id=t.id,
        exchange=t.exchange,
        symbol=t.symbol,
        side=t.side,
        entry_price=t.entry_price,
        exit_price=t.exit_price,
        quantity=t.quantity,
        leverage=t.leverage,
        pnl_usdt=round(t.pnl_usdt, 2),
        mode=t.mode,
        opened_at=t.opened_at,
        closed_at=t.closed_at,
    )
