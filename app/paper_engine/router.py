"""
Sprint 20B — per-user paper-trading API.

Mounted only when PAPER_TRADING_ENABLED=true. All routes require an
authenticated user (Sprint 20A), so AUTH_ENABLED must also be on for the
endpoints to be reachable. Routes live under /api/paper/account/* to avoid
colliding with the legacy global paper engine at /api/paper{,/positions,/stats}.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.auth.deps import get_current_user
from app.database.models import AuthUser, PaperAccountPosition, Signal
from app.database.session import get_session
from app.paper_engine import math as pmath
from app.paper_engine import service
from app.paper_engine.schemas import (
    AccountSummaryOut,
    AutoFollowIn,
    ClosePositionIn,
    FromSignalIn,
    MessageOut,
    OpenPositionIn,
    OrderOut,
    PositionOut,
    SimulationOut,
    TradeOut,
)

router = APIRouter(prefix="/api/paper/account", tags=["paper"])

# Diagnostics router (no prefix) — surfaces the exact mark/ROE/PnL inputs so a
# "0% ROE" report can be traced to its price source in one call.
debug_router = APIRouter(prefix="/api/debug", tags=["paper-debug"])


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _err(exc: service.PaperError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def _position_out(p: PaperAccountPosition, with_mark: bool = True) -> PositionOut:
    mark = None
    upnl = roe = None
    if with_mark and p.status == "OPEN":
        live = service.mark_price(p.symbol)  # 0.0 if no live mark — never entry
        if live > 0:
            upnl = round(pmath.unrealized_pnl(p.side, p.entry_price, live, p.notional_usdt), 2)
            roe = round(pmath.roe_pct(upnl, p.margin_usdt), 2)
            mark = round(live, 8)
    return PositionOut(
        id=p.id,
        symbol=p.symbol,
        side=p.side,
        entry_price=p.entry_price,
        quantity=round(p.quantity, 8),
        notional_usdt=round(p.notional_usdt, 2),
        leverage=p.leverage,
        margin_usdt=round(p.margin_usdt, 2),
        liquidation_price=round(p.liquidation_price, 8),
        stop_loss=p.stop_loss,
        tp1=p.tp1,
        tp2=p.tp2,
        tp3=p.tp3,
        status=p.status,
        mark_price=mark,
        unrealized_pnl=upnl,
        roe_pct=roe,
        realized_pnl_usdt=round(p.realized_pnl_usdt, 2),
        funding_usdt=round(p.funding_usdt, 4),
        signal_id=p.signal_id,
        opened_at=p.opened_at,
        closed_at=p.closed_at,
    )


# ── account ───────────────────────────────────────────────────────


@router.get("/", response_model=AccountSummaryOut)
async def get_account(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        acc = await service.get_or_create_account(db, user.id)
        return AccountSummaryOut(**await service.account_summary(db, acc))


@router.post("/reset", response_model=AccountSummaryOut)
async def reset_account(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        acc = await service.get_or_create_account(db, user.id)
        await service.reset_account(db, acc)
        return AccountSummaryOut(**await service.account_summary(db, acc))


@router.post("/auto-follow", response_model=MessageOut)
async def auto_follow(body: AutoFollowIn, user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        acc = await service.get_or_create_account(db, user.id)
        await service.set_auto_follow(db, acc, body.enabled)
    return MessageOut(detail=f"auto-follow {'enabled' if body.enabled else 'disabled'}")


# ── open / copy / simulate ────────────────────────────────────────


@router.post("/open", response_model=PositionOut, status_code=201)
async def open_position(body: OpenPositionIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            acc = await service.get_or_create_account(db, user.id)
            pos = await service.open_position(
                db,
                acc,
                symbol=body.symbol.upper(),
                side=body.side,
                entry_price=body.entry_price,
                leverage=body.leverage,
                margin_usdt=body.margin_usdt,
                notional_usdt=body.notional_usdt,
                stop_loss=body.stop_loss,
                tp1=body.tp1,
                tp2=body.tp2,
                tp3=body.tp3,
                order_type=body.order_type,
            )
            await service.ensure_marks([pos.symbol])
            return _position_out(pos)
    except service.PaperError as exc:
        return _err(exc)


@router.post("/copy", response_model=PositionOut, status_code=201)
async def copy_signal(body: FromSignalIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            sig = await db.get(Signal, body.signal_id)
            if sig is None:
                raise service.PaperError(404, "Signal not found")
            acc = await service.get_or_create_account(db, user.id)
            pos = await service.copy_signal(db, acc, sig, leverage=body.leverage)
            await service.ensure_marks([pos.symbol])
            return _position_out(pos)
    except service.PaperError as exc:
        return _err(exc)


@router.post("/simulate", response_model=SimulationOut)
async def simulate(body: FromSignalIn, user: AuthUser = Depends(get_current_user)):
    try:
        async with get_session() as db:
            sig = await db.get(Signal, body.signal_id)
            if sig is None:
                raise service.PaperError(404, "Signal not found")
            acc = await service.get_or_create_account(db, user.id)
            return SimulationOut(**service.simulate_signal(acc, sig, leverage=body.leverage))
    except service.PaperError as exc:
        return _err(exc)


@router.post("/positions/{position_id}/close", response_model=TradeOut)
async def close_position(
    position_id: int, body: ClosePositionIn, user: AuthUser = Depends(get_current_user)
):
    try:
        async with get_session() as db:
            acc = await service.get_or_create_account(db, user.id)
            trade = await service.close_position(
                db, acc, position_id, mark=body.mark_price, reason=body.reason
            )
            return TradeOut(
                id=trade.id,
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                quantity=round(trade.quantity, 8),
                notional_usdt=round(trade.notional_usdt, 2),
                leverage=trade.leverage,
                pnl_usdt=round(trade.pnl_usdt, 2),
                pnl_pct=round(trade.pnl_pct, 2),
                funding_usdt=round(trade.funding_usdt, 4),
                reason=trade.reason,
                signal_id=trade.signal_id,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
            )
    except service.PaperError as exc:
        return _err(exc)


# ── listings ──────────────────────────────────────────────────────


@router.get("/positions", response_model=list[PositionOut])
async def positions(
    status: str = Query(default="", pattern="^(open|closed|)$"),
    user: AuthUser = Depends(get_current_user),
):
    async with get_session() as db:
        acc = await service.get_or_create_account(db, user.id)
        rows = await service.list_positions(db, acc.id, status=status or None)
        await service.ensure_marks([p.symbol for p in rows if p.status == "OPEN"])
        return [_position_out(p) for p in rows]


@debug_router.get("/paper-positions")
async def debug_paper_positions(user: AuthUser = Depends(get_current_user)):
    """Per-open-position mark/ROE/PnL with its live price source — for debugging."""
    async with get_session() as db:
        acc = await service.get_or_create_account(db, user.id)
        rows = await service.list_positions(db, acc.id, status="open")
        await service.ensure_marks([p.symbol for p in rows])
        out = []
        for p in rows:
            mark, source, age = service.mark_price_info(p.symbol)
            pnl: float | None
            roe: float | None
            if mark > 0:
                pnl = round(pmath.unrealized_pnl(p.side, p.entry_price, mark, p.notional_usdt), 4)
                roe = round(pmath.roe_pct(pnl, p.margin_usdt), 4)
            else:
                pnl = roe = None
            out.append(
                {
                    "id": p.id,
                    "symbol": p.symbol,
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "mark_price": round(mark, 8) if mark > 0 else None,
                    "qty": round(p.quantity, 8),
                    "leverage": p.leverage,
                    "notional_usdt": round(p.notional_usdt, 4),
                    "margin_usdt": round(p.margin_usdt, 4),
                    "roe": roe,
                    "pnl": pnl,
                    "price_source": source,
                    "last_price_update": round(age, 3) if age is not None else None,
                }
            )
        return {
            "account_id": acc.id,
            "open_positions": len(out),
            "server_time": _now_iso(),
            "positions": out,
        }


@router.get("/orders", response_model=list[OrderOut])
async def orders(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        acc = await service.get_or_create_account(db, user.id)
        rows = await service.list_orders(db, acc.id)
        return [
            OrderOut(
                id=o.id,
                symbol=o.symbol,
                side=o.side,
                order_type=o.order_type,
                price=o.price,
                quantity=round(o.quantity, 8),
                notional_usdt=round(o.notional_usdt, 2),
                reduce_only=o.reduce_only,
                status=o.status,
                position_id=o.position_id,
                created_at=o.created_at,
                filled_at=o.filled_at,
            )
            for o in rows
        ]


@router.get("/trades", response_model=list[TradeOut])
async def trades(user: AuthUser = Depends(get_current_user)):
    async with get_session() as db:
        acc = await service.get_or_create_account(db, user.id)
        rows = await service.list_trades(db, acc.id)
        return [
            TradeOut(
                id=t.id,
                symbol=t.symbol,
                side=t.side,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                quantity=round(t.quantity, 8),
                notional_usdt=round(t.notional_usdt, 2),
                leverage=t.leverage,
                pnl_usdt=round(t.pnl_usdt, 2),
                pnl_pct=round(t.pnl_pct, 2),
                funding_usdt=round(t.funding_usdt, 4),
                reason=t.reason,
                signal_id=t.signal_id,
                opened_at=t.opened_at,
                closed_at=t.closed_at,
            )
            for t in rows
        ]
