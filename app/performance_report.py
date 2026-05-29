import asyncio
from sqlalchemy import text

from app.database.session import SessionLocal


async def q(sql: str):
    async with SessionLocal() as session:
        r = await session.execute(text(sql))
        return r.fetchall()


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
        "WORST SYMBOLS": """
            SELECT symbol, COUNT(*) total, 
                   SUM(CASE WHEN status='SL' THEN 1 ELSE 0 END) losses,
                   ROUND(AVG(pnl_pct)::numeric, 2) avg_pnl
            FROM signals
            WHERE status != 'OPEN'
            GROUP BY symbol
            HAVING COUNT(*) >= 2
            ORDER BY avg_pnl ASC
            LIMIT 15;
        """,
        "BEST SYMBOLS": """
            SELECT symbol, COUNT(*) total, 
                   SUM(CASE WHEN status IN ('TP1','TP2','TP3') THEN 1 ELSE 0 END) wins,
                   ROUND(AVG(pnl_pct)::numeric, 2) avg_pnl
            FROM signals
            WHERE status != 'OPEN'
            GROUP BY symbol
            HAVING COUNT(*) >= 2
            ORDER BY avg_pnl DESC
            LIMIT 15;
        """,
    }

    for name, sql in sections.items():
        print("\\n" + "=" * 60)
        print(name)
        print("=" * 60)
        rows = await q(sql)
        for row in rows:
            print(tuple(row))


asyncio.run(main())
