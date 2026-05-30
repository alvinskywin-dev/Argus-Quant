"""
Sprint 2 — Performance Rebuild CLI
====================================
scripts/rebuild_performance.py

Recalculates performance metrics using ONLY:
    strategy = MTF_SMC_STRICT
    timeframes: 15m / 1H / 4H / 1D

Ignores:
    - archived signals  (in archive_signals table)
    - legacy signals    (strategy != MTF_SMC_STRICT)
    - 5m signals        (timeframe = 5m)

Rebuilds:
    - Win Rate
    - Average PnL
    - Profit Factor
    - Average RR
    - Signal Count (total / closed / open / wins / losses)
    - daily_stats  rows
    - weekly_stats rows

Usage:
    python scripts/rebuild_performance.py
    docker compose exec bot python scripts/rebuild_performance.py
"""
from __future__ import annotations

import asyncio
import os
import sys

# Make app/ importable when run as a standalone script outside the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.performance.rebuild import rebuild


def _sep(width: int = 60) -> None:
    print("=" * width)


async def main() -> None:
    _sep()
    print("  ALPHA RADAR SIGNALS — PERFORMANCE REBUILD")
    _sep()
    print()

    result = await rebuild()

    sc = result["signal_count"]

    print("  Signal Count:")
    print(f"    Total signals:   {sc['total']}")
    print(f"    Closed:          {sc['closed']}")
    print(f"    Open (active):   {sc['open']}")
    print(f"    Wins:            {sc['wins']}")
    print(f"    Losses:          {sc['losses']}")
    print()
    print("  Performance Metrics (MTF signals only):")
    print(f"    Win Rate:        {result['win_rate']}%")
    print(f"    Avg PnL:         {result['avg_pnl']:+.2f}%")
    print(f"    Profit Factor:   {result['profit_factor']}")
    print(f"    Avg RR:          1:{result['avg_rr']}")
    print()
    print("  Stats Tables Rebuilt:")
    print(f"    daily_stats:     {result['daily_rows']} rows")
    print(f"    weekly_stats:    {result['weekly_rows']} rows")
    print()
    print(f"  Rebuilt at: {result['rebuilt_at']}")
    print()
    print("  ✅ Performance rebuild complete.")
    _sep()


if __name__ == "__main__":
    asyncio.run(main())
