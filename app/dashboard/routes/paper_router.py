"""paper router — extracted from server.py (Phase 4).

Handlers moved verbatim; shared helpers/views/templates are imported
from app.dashboard.server. Wired via create_app().include_router().
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from app.dashboard.server import (
    _MTF_STRATEGY,
    _MTF_TIMEFRAMES,
    _paper_page_html,
)
from app.database.models import Signal
from app.database.session import SessionLocal
from app.utils.timezone import normalize_utc_iso

router = APIRouter()


@router.get("/api/paper")
async def api_paper():
    """Paper trading — stats + latest positions (Sprint 6 primary endpoint)."""
    try:
        from app.paper.trading import get_portfolio_stats, get_positions

        stats = await get_portfolio_stats()
        open_pos = await get_positions(status="open", limit=50)
        closed_pos = await get_positions(status="closed", limit=50)
        return JSONResponse({**stats, "open": open_pos, "closed": closed_pos})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/paper/positions")
async def api_paper_positions(status: str = "", limit: int = 100):
    """Paper trading — positions list (filter: open / closed / all)."""
    limit = min(max(1, limit), 500)
    try:
        from app.paper.trading import get_positions

        rows = await get_positions(
            status=status.lower() if status else None,
            limit=limit,
        )
        return JSONResponse({"positions": rows, "count": len(rows)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/paper/stats")
async def api_paper_stats():
    """Paper trading — portfolio statistics only."""
    try:
        from app.paper.trading import get_portfolio_stats

        return JSONResponse(await get_portfolio_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/paper")
async def api_public_paper():
    """Paper trading portfolio derived from all MTF signals (virtual 10 000 USDT)."""
    INITIAL_BALANCE = 10_000.0
    RISK_PCT = 0.01  # 1% per trade

    try:
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
                .order_by(Signal.created_at)
                .limit(500)
            )
            signals = res.scalars().all()

        open_pos = [s for s in signals if s.status == "OPEN"]
        closed_pos = [s for s in signals if s.status in ("TP1", "TP2", "TP3", "SL")]

        running_balance = INITIAL_BALANCE
        closed_rows = []
        for s in closed_pos:
            size = running_balance * RISK_PCT
            pnl_usdt = round(size * float(s.pnl_pct or 0) / 100, 2)
            running_balance += pnl_usdt
            closed_rows.append(
                {
                    "id": s.id,
                    "symbol": s.symbol,
                    "side": s.side,
                    "entry": round(float(s.entry_low or 0), 6),
                    "sl": round(float(s.stop_loss or 0), 6),
                    "tp1": round(float(s.tp1 or 0), 6),
                    "status": s.status,
                    "pnl_pct": round(float(s.pnl_pct or 0), 2),
                    "pnl_usdt": pnl_usdt,
                    "opened": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
                    "opened_iso": normalize_utc_iso(s.created_at),
                }
            )

        open_rows = [
            {
                "id": s.id,
                "symbol": s.symbol,
                "side": s.side,
                "entry": round(float(s.entry_low or 0), 6),
                "sl": round(float(s.stop_loss or 0), 6),
                "tp1": round(float(s.tp1 or 0), 6),
                "conf": round(float(s.confidence or 0), 1),
                "rr": round(float(s.risk_reward or 0), 2),
                "opened": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
                "opened_iso": normalize_utc_iso(s.created_at),
            }
            for s in open_pos
        ]

        wins = [r for r in closed_rows if r["status"] in ("TP1", "TP2", "TP3")]
        total_pnl_usdt = round(sum(r["pnl_usdt"] for r in closed_rows), 2)
        win_rate = round(len(wins) / max(1, len(closed_rows)) * 100, 1)

        return JSONResponse(
            {
                "initial_balance": INITIAL_BALANCE,
                "current_balance": round(running_balance, 2),
                "total_pnl_usdt": total_pnl_usdt,
                "total_pnl_pct": round(
                    (running_balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2
                ),
                "win_rate": win_rate,
                "total_trades": len(closed_rows),
                "open_count": len(open_rows),
                "open": open_rows,
                "closed": list(reversed(closed_rows))[:50],
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/paper", response_class=HTMLResponse)
async def paper_page():
    return HTMLResponse(_paper_page_html())
