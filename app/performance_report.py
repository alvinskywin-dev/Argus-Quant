import asyncio
from collections import defaultdict

from sqlalchemy import select, text

from app.analytics.trade_outcome import BUCKET_LOSS, BUCKET_WIN, winrate_bucket_for_signal
from app.database.models import Signal
from app.database.session import SessionLocal


async def q(sql: str):
    async with SessionLocal() as session:
        r = await session.execute(text(sql))
        return r.fetchall()


async def _symbol_rows():
    """Per-symbol win/loss tallies — lifecycle-aware (a TP-then-SL trade is a
    win, not a loss), so figures match the dashboard rather than raw status."""
    async with SessionLocal() as session:
        result = await session.execute(select(Signal).where(Signal.status != "OPEN"))
        signals = result.scalars().all()

    by_symbol: dict = defaultdict(list)
    for s in signals:
        by_symbol[s.symbol].append(s)

    rows = []
    for symbol, sigs in by_symbol.items():
        if len(sigs) < 2:
            continue
        wins = sum(1 for s in sigs if winrate_bucket_for_signal(s) == BUCKET_WIN)
        losses = sum(1 for s in sigs if winrate_bucket_for_signal(s) == BUCKET_LOSS)
        pnls = [float(s.pnl_pct) for s in sigs if s.pnl_pct is not None]
        avg_pnl = round(sum(pnls) / len(pnls), 2) if pnls else 0.0
        rows.append(
            {
                "symbol": symbol,
                "total": len(sigs),
                "wins": wins,
                "losses": losses,
                "avg_pnl": avg_pnl,
            }
        )
    return rows


async def main():
    sections = {
        "STATUS SUMMARY": """
            SELECT status, COUNT(*) total, ROUND(AVG(pnl_pct)::numeric, 2) avg_pnl
            FROM signals
            GROUP BY status
            ORDER BY total DESC;
        """,
        "BY SIDE": """
            SELECT side, status, COUNT(*) total, ROUND(AVG(pnl_pct)::numeric, 2) avg_pnl
            FROM signals
            GROUP BY side, status
            ORDER BY side, total DESC;
        """,
        "BY TIMEFRAME": """
            SELECT timeframe, status, COUNT(*) total, ROUND(AVG(pnl_pct)::numeric, 2) avg_pnl
            FROM signals
            GROUP BY timeframe, status
            ORDER BY timeframe, total DESC;
        """,
    }

    for name, sql in sections.items():
        print("\\n" + "=" * 60)
        print(name)
        print("=" * 60)
        rows = await q(sql)
        for row in rows:
            print(tuple(row))

    sym_rows = await _symbol_rows()

    print("\\n" + "=" * 60)
    print("WORST SYMBOLS")
    print("=" * 60)
    for r in sorted(sym_rows, key=lambda x: x["avg_pnl"])[:15]:
        print((r["symbol"], r["total"], r["losses"], r["avg_pnl"]))

    print("\\n" + "=" * 60)
    print("BEST SYMBOLS")
    print("=" * 60)
    for r in sorted(sym_rows, key=lambda x: x["avg_pnl"], reverse=True)[:15]:
        print((r["symbol"], r["total"], r["wins"], r["avg_pnl"]))


asyncio.run(main())
