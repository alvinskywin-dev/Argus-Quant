"""
Performance rebuild engine — Sprint 2.

Shared module imported by both the CLI script and the API endpoint.
Queries only MTF_SMC_STRICT signals on 15m/1h/4h/1d timeframes;
ignores all archived, legacy, and 5m signals.

Computes:
    Win Rate        — wins / closed * 100
    Average PnL     — mean pnl_pct of closed signals
    Profit Factor   — gross wins / gross losses (by pnl_pct)
    Average RR      — mean risk_reward of closed signals
    Signal Count    — total / closed / open / wins / losses

Rebuilds:
    daily_stats     — one row per calendar day
    weekly_stats    — one row per ISO week (YYYY-WNN)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from app.database.models import DailyStat, Signal, WeeklyStat
from app.database.session import SessionLocal

MTF_TIMEFRAMES: list[str] = ["15m", "1h", "4h", "1d"]
MTF_STRATEGY:   str       = "MTF_SMC_STRICT"


async def rebuild() -> dict[str, Any]:
    """
    Run a full performance rebuild.

    Returns a dict with keys:
        status, rebuilt_at, signal_count,
        win_rate, avg_pnl, profit_factor, avg_rr,
        daily_rows, weekly_rows
    """
    # ── 1. Fetch MTF signals ──────────────────────────────────────
    async with SessionLocal() as session:
        closed_res = await session.execute(
            select(Signal)
            .where(
                Signal.strategy == MTF_STRATEGY,
                Signal.timeframe.in_(MTF_TIMEFRAMES),
                Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
            )
            .order_by(Signal.created_at)
        )
        closed: list[Signal] = list(closed_res.scalars().all())

        open_res = await session.execute(
            select(Signal)
            .where(
                Signal.strategy == MTF_STRATEGY,
                Signal.timeframe.in_(MTF_TIMEFRAMES),
                Signal.status == "OPEN",
            )
        )
        open_signals: list[Signal] = list(open_res.scalars().all())

    wins   = [s for s in closed if s.status in ("TP1", "TP2", "TP3")]
    losses = [s for s in closed if s.status == "SL"]
    pnls   = [float(s.pnl_pct   or 0) for s in closed]
    rrs    = [float(s.risk_reward or 0) for s in closed]

    n = len(closed)

    # ── 2. Compute 5 metrics ──────────────────────────────────────
    win_rate = round(len(wins) / max(1, n) * 100, 1)
    avg_pnl  = round(sum(pnls) / max(1, n), 2)
    avg_rr   = round(sum(rrs)  / max(1, n), 2)

    gross_win  = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = round(gross_win / max(0.001, gross_loss), 2)

    signal_count: dict[str, int] = {
        "total":  n + len(open_signals),
        "closed": n,
        "open":   len(open_signals),
        "wins":   len(wins),
        "losses": len(losses),
    }

    # ── 3. Rebuild daily_stats ────────────────────────────────────
    daily: dict[str, list[Signal]] = defaultdict(list)
    for s in closed:
        if s.created_at:
            daily[s.created_at.strftime("%Y-%m-%d")].append(s)

    async with SessionLocal() as session:
        await session.execute(delete(DailyStat))
        for day_key, day_sigs in sorted(daily.items()):
            d_wins = [s for s in day_sigs if s.status in ("TP1", "TP2", "TP3")]
            d_pnls = [float(s.pnl_pct or 0) for s in day_sigs]
            session.add(DailyStat(
                day=day_key,
                signals_total=len(day_sigs),
                wins=len(d_wins),
                losses=len(day_sigs) - len(d_wins),
                avg_pnl=round(sum(d_pnls) / max(1, len(d_pnls)), 2),
                best_pnl=round(max(d_pnls, default=0.0), 2),
                worst_pnl=round(min(d_pnls, default=0.0), 2),
            ))
        await session.commit()

    daily_rows = len(daily)

    # ── 4. Rebuild weekly_stats ───────────────────────────────────
    weekly: dict[str, list[Signal]] = defaultdict(list)
    for s in closed:
        if s.created_at:
            yr, wk, _ = s.created_at.isocalendar()
            weekly[f"{yr}-W{wk:02d}"].append(s)

    async with SessionLocal() as session:
        await session.execute(delete(WeeklyStat))
        for week_key, week_sigs in sorted(weekly.items()):
            w_wins = [s for s in week_sigs if s.status in ("TP1", "TP2", "TP3")]
            w_pnls = [float(s.pnl_pct or 0) for s in week_sigs]
            wn     = len(week_sigs)
            session.add(WeeklyStat(
                week=week_key,
                signals_total=wn,
                wins=len(w_wins),
                losses=wn - len(w_wins),
                win_rate=round(len(w_wins) / max(1, wn) * 100, 1),
                avg_pnl=round(sum(w_pnls) / max(1, len(w_pnls)), 2),
                best_pnl=round(max(w_pnls, default=0.0), 2),
                worst_pnl=round(min(w_pnls, default=0.0), 2),
            ))
        await session.commit()

    weekly_rows = len(weekly)

    return {
        "status":         "ok",
        "rebuilt_at":     datetime.now(timezone.utc).isoformat(),
        "signal_count":   signal_count,
        "win_rate":       win_rate,
        "avg_pnl":        avg_pnl,
        "profit_factor":  profit_factor,
        "avg_rr":         avg_rr,
        "daily_rows":     daily_rows,
        "weekly_rows":    weekly_rows,
    }
