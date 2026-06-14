"""
Paper Trading Engine — Sprint 6.

Simulates a virtual futures portfolio at zero real risk.
One paper position is created for every valid MTF signal.

Constants
---------
INITIAL_BALANCE  10 000 USDT
RISK_PCT         1 % of current balance per trade

Position sizing
---------------
risk_usdt    = current_balance × RISK_PCT
risk_dist    = |entry_price − stop_loss| / entry_price   (fraction)
size_usdt    = risk_usdt / risk_dist

This gives constant monetary risk per trade regardless of leverage or
price level.  Minimum size is capped at risk_usdt (degenerate stop case).

Lifecycle
---------
open  → status = OPEN
TP1   → status = TP1  (partial milestone, position still tracking)
TP2   → status = TP2
TP3   → status = TP3, closed_at set, pnl realised
SL    → status = SL,  closed_at set, pnl realised (negative)

Win rate counts TP1/TP2/TP3 as wins, SL as loss.
Balance curve uses only TP3/SL closed positions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select, update

from app.database.models import PaperPosition, Signal
from app.database.session import SessionLocal
from app.paper_engine.math import risk_based_notional

# ── constants ────────────────────────────────────────────────────────────────

INITIAL_BALANCE: float = 10_000.0
RISK_PCT: float = 0.01  # 1 % per trade
FINAL_STATUSES = ("TP3", "SL")  # positions that close the trade
WIN_STATUSES = ("TP1", "TP2", "TP3")


# ── helpers ──────────────────────────────────────────────────────────────────


def _calc_size(entry: float, stop_loss: float, balance: float) -> float:
    """Return position size in USDT using risk-based sizing.

    The core maths — risk RISK_PCT of balance over the entry→stop distance,
    capped at 50% of balance — is delegated to the shared paper_engine helper
    (single source of truth). This wrapper preserves the module's exact rounding
    and the degenerate-stop fallback (return the bare risk amount when there is
    no usable stop distance). ``leverage=1`` makes the notional cap equal
    ``0.5 * balance`` to match the original behaviour.
    """
    risk_usdt = balance * RISK_PCT
    if entry <= 0 or stop_loss <= 0 or entry == stop_loss:
        return round(risk_usdt, 2)
    notional = risk_based_notional(
        balance, RISK_PCT * 100.0, entry, stop_loss, leverage=1, max_notional_frac=0.5
    )
    return round(notional, 2)


async def _current_balance() -> float:
    """
    Replay all final-closed paper positions in chronological order
    to produce the current running virtual balance.
    """
    balance = INITIAL_BALANCE
    async with SessionLocal() as s:
        res = await s.execute(
            select(PaperPosition.pnl_usdt)
            .where(PaperPosition.status.in_(FINAL_STATUSES))
            .order_by(PaperPosition.closed_at)
        )
        for (pnl,) in res.all():
            balance += float(pnl or 0)
    return balance


# ── public API ────────────────────────────────────────────────────────────────


async def open_paper_position(signal: Signal) -> PaperPosition | None:
    """
    Create a new paper position for *signal*.
    Returns the new PaperPosition, or None if sizing fails.
    """
    try:
        entry = float(signal.entry_low or 0)
        stop_loss = float(signal.stop_loss or 0)
        tp1 = float(signal.tp1 or 0)
        tp2 = float(signal.tp2 or 0)
        tp3 = float(signal.tp3 or 0)

        if entry <= 0:
            return None

        balance = await _current_balance()
        size_usdt = _calc_size(entry, stop_loss, balance)

        pos = PaperPosition(
            signal_id=signal.id,
            symbol=signal.symbol,
            side=signal.side,
            entry_price=entry,
            stop_loss=stop_loss,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            size_usdt=size_usdt,
            status="OPEN",
            pnl_usdt=0.0,
            pnl_pct=0.0,
        )

        async with SessionLocal() as s:
            s.add(pos)
            await s.commit()
            await s.refresh(pos)

        return pos

    except Exception:
        return None


async def on_signal_event(signal_id: int, event: str, pnl_pct: float) -> None:
    """
    Called by the signal tracker whenever a TP or SL event fires.

    TP1 / TP2: update status only — trade continues tracking.
    TP3 / SL:  close the position, realise PnL, set closed_at.
    """
    if event not in ("TP1", "TP2", "TP3", "SL"):
        return

    async with SessionLocal() as s:
        # Find the matching open paper position
        res = await s.execute(
            select(PaperPosition)
            .where(
                PaperPosition.signal_id == signal_id,
                PaperPosition.status.not_in(FINAL_STATUSES),
            )
            .limit(1)
        )
        pos: PaperPosition | None = res.scalar_one_or_none()
        if pos is None:
            return

        fields: dict[str, Any] = {"status": event, "pnl_pct": round(pnl_pct, 3)}

        if event in FINAL_STATUSES:
            pnl_usdt = round(float(pos.size_usdt) * pnl_pct / 100, 2)
            fields["pnl_usdt"] = pnl_usdt
            fields["closed_at"] = datetime.now(timezone.utc)

        await s.execute(update(PaperPosition).where(PaperPosition.id == pos.id).values(**fields))
        await s.commit()


# ── stats & query helpers ─────────────────────────────────────────────────────


async def get_portfolio_stats() -> dict[str, Any]:
    """Compute full portfolio statistics from the paper_positions table."""
    async with SessionLocal() as s:
        all_res = await s.execute(select(PaperPosition).order_by(PaperPosition.opened_at))
        all_pos: list[PaperPosition] = list(all_res.scalars().all())

    if not all_pos:
        return {
            "initial_balance": INITIAL_BALANCE,
            "current_balance": INITIAL_BALANCE,
            "total_pnl_usdt": 0.0,
            "total_pnl_pct": 0.0,
            "total_trades": 0,
            "open_count": 0,
            "closed_count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_pnl_pct": 0.0,
            "balance_curve": [INITIAL_BALANCE],
        }

    open_pos = [p for p in all_pos if p.status == "OPEN"]
    final_pos = [p for p in all_pos if p.status in FINAL_STATUSES]
    wins = [p for p in final_pos if p.status in WIN_STATUSES]
    losses = [p for p in final_pos if p.status == "SL"]
    partial = [p for p in all_pos if p.status in ("TP1", "TP2")]

    # running balance
    balance = INITIAL_BALANCE
    curve = [INITIAL_BALANCE]
    for p in final_pos:
        balance += float(p.pnl_usdt or 0)
        curve.append(round(balance, 2))

    total_pnl = round(balance - INITIAL_BALANCE, 2)
    n_closed = len(final_pos)
    pnls = [float(p.pnl_pct or 0) for p in final_pos]

    return {
        "initial_balance": INITIAL_BALANCE,
        "current_balance": round(balance, 2),
        "total_pnl_usdt": total_pnl,
        "total_pnl_pct": round(total_pnl / INITIAL_BALANCE * 100, 2),
        "total_trades": len(all_pos),
        "open_count": len(open_pos),
        "closed_count": n_closed,
        "partial_count": len(partial),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / max(1, n_closed) * 100, 1),
        "avg_pnl_pct": round(sum(pnls) / max(1, len(pnls)), 2),
        "balance_curve": curve[-60:],  # last 60 data points
    }


async def get_positions(
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return a list of paper positions as dicts, newest first."""
    async with SessionLocal() as s:
        q = select(PaperPosition)
        if status:
            if status == "open":
                q = q.where(PaperPosition.status.not_in(FINAL_STATUSES))
            elif status == "closed":
                q = q.where(PaperPosition.status.in_(FINAL_STATUSES))
        q = q.order_by(desc(PaperPosition.opened_at)).limit(limit)
        res = await s.execute(q)
        rows = res.scalars().all()

    def _fmt(dt: datetime | None) -> str | None:
        return dt.strftime("%m-%d %H:%M") if dt else None

    return [
        {
            "id": p.id,
            "signal_id": p.signal_id,
            "symbol": p.symbol,
            "side": p.side,
            "entry_price": round(float(p.entry_price), 6),
            "stop_loss": round(float(p.stop_loss), 6),
            "tp1": round(float(p.tp1), 6),
            "tp2": round(float(p.tp2), 6),
            "tp3": round(float(p.tp3), 6),
            "size_usdt": round(float(p.size_usdt), 2),
            "status": p.status,
            "pnl_usdt": round(float(p.pnl_usdt or 0), 2),
            "pnl_pct": round(float(p.pnl_pct or 0), 2),
            "opened_at": _fmt(p.opened_at),
            "closed_at": _fmt(p.closed_at),
        }
        for p in rows
    ]
