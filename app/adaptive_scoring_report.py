import asyncio
import os
from datetime import datetime, timezone

from sqlalchemy import text

from app.database.session import SessionLocal


async def main():
    prod_start_raw = os.getenv("PRODUCTION_START_UTC", "").strip()
    since_filter = ""
    params = {}

    if prod_start_raw:
        try:
            since = datetime.strptime(prod_start_raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            since_filter = "AND created_at >= :since"
            params["since"] = since
        except Exception:
            pass

    sql = f"""
    SELECT
        side,
        timeframe,
        risk_level,
        COUNT(*) total,
        SUM(CASE WHEN status IN ('TP1','TP2','TP3') THEN 1 ELSE 0 END) wins,
        SUM(CASE WHEN status='SL' THEN 1 ELSE 0 END) losses,
        ROUND(AVG(confidence)::numeric, 2) avg_conf,
        ROUND(AVG(risk_reward)::numeric, 2) avg_rr,
        ROUND(AVG(pnl_pct)::numeric, 2) avg_pnl
    FROM signals
    WHERE status != 'OPEN' {since_filter}
    GROUP BY side, timeframe, risk_level
    HAVING COUNT(*) >= 3
    ORDER BY avg_pnl DESC;
    """

    async with SessionLocal() as session:
        rows = (await session.execute(text(sql), params)).fetchall()

    print("\n🧠 ARGUS QUANT ADAPTIVE SCORING REPORT")
    print("=" * 70)

    if not rows:
        print("Not enough closed production signals yet.")
        return

    for r in rows:
        total = r.total or 0
        wins = r.wins or 0
        losses = r.losses or 0
        winrate = round(wins / max(wins + losses, 1) * 100, 1)

        print(
            f"{r.side} {r.timeframe} {r.risk_level} | "
            f"total={total} winrate={winrate}% "
            f"avg_conf={r.avg_conf} avg_rr={r.avg_rr} avg_pnl={r.avg_pnl}"
        )


if __name__ == "__main__":
    asyncio.run(main())
