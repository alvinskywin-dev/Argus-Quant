"""
Phase 2 — Performance Recalculation (V3.1 Enterprise Cleanup).

Rebuilds DailyStat rows from scratch using only MTF_SMC_STRICT signals
(timeframe IN 15m/1h/4h/1d). Discards any stats polluted by legacy 5m data.

Usage:
    python scripts/rebuild_performance.py
    # or inside docker:
    docker compose exec bot python scripts/rebuild_performance.py
"""
from __future__ import annotations

import asyncio
import math
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from collections import defaultdict

from sqlalchemy import select, delete

from app.database.models import DailyStat, Signal
from app.database.session import SessionLocal


MTF_TIMEFRAMES = {"15m", "1h", "4h", "1d"}
MTF_STRATEGY   = "MTF_SMC_STRICT"


async def rebuild() -> None:
    print("=" * 60)
    print("  ALPHA RADAR SIGNALS — PERFORMANCE REBUILD")
    print("=" * 60)

    async with SessionLocal() as session:
        result = await session.execute(
            select(Signal).where(
                Signal.strategy == MTF_STRATEGY,
                Signal.timeframe.in_(list(MTF_TIMEFRAMES)),
                Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
            ).order_by(Signal.created_at)
        )
        signals: list[Signal] = list(result.scalars().all())

    if not signals:
        print("No closed MTF signals found. Nothing to rebuild.")
        return

    print(f"Found {len(signals)} closed MTF signals.")

    wins = [s for s in signals if s.status in ("TP1", "TP2", "TP3")]
    losses = [s for s in signals if s.status == "SL"]
    pnls = [float(s.pnl_pct or 0) for s in signals]

    win_rate = len(wins) / max(1, len(signals)) * 100
    avg_pnl = sum(pnls) / max(1, len(pnls))

    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = gross_win / max(0.001, gross_loss)

    if len(pnls) > 1:
        mean = sum(pnls) / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        sharpe = round(mean / max(0.001, math.sqrt(variance)), 2)
    else:
        sharpe = 0.0

    print()
    print("  Overall Performance (MTF signals only):")
    print(f"    Total closed:   {len(signals)}")
    print(f"    Wins:           {len(wins)}")
    print(f"    Losses:         {len(losses)}")
    print(f"    Win Rate:       {win_rate:.1f}%")
    print(f"    Avg PnL:        {avg_pnl:+.2f}%")
    print(f"    Profit Factor:  {profit_factor:.2f}")
    print(f"    Sharpe Ratio:   {sharpe}")
    print()

    # Group by day
    daily: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        if s.created_at:
            day = s.created_at.strftime("%Y-%m-%d")
            daily[day].append(s)

    # Rebuild DailyStat rows
    async with SessionLocal() as session:
        # Clear existing daily_stats
        await session.execute(delete(DailyStat))
        await session.commit()

    new_rows = 0
    async with SessionLocal() as session:
        for day, day_sigs in sorted(daily.items()):
            day_wins = [s for s in day_sigs if s.status in ("TP1", "TP2", "TP3")]
            day_losses = [s for s in day_sigs if s.status == "SL"]
            day_pnls = [float(s.pnl_pct or 0) for s in day_sigs]
            row = DailyStat(
                day=day,
                signals_total=len(day_sigs),
                wins=len(day_wins),
                losses=len(day_losses),
                avg_pnl=round(sum(day_pnls) / max(1, len(day_pnls)), 2),
                best_pnl=round(max(day_pnls, default=0.0), 2),
                worst_pnl=round(min(day_pnls, default=0.0), 2),
            )
            session.add(row)
            new_rows += 1
        await session.commit()

    print(f"  Rebuilt {new_rows} DailyStat rows from MTF signals.")
    print()
    print("✅ Performance rebuild complete.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(rebuild())
