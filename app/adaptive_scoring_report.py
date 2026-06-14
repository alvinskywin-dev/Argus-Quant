import asyncio
import os
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from app.analytics.trade_outcome import BUCKET_LOSS, BUCKET_WIN, winrate_bucket_for_signal
from app.database.models import Signal
from app.database.session import SessionLocal


def _avg(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


async def main():
    prod_start_raw = os.getenv("PRODUCTION_START_UTC", "").strip()
    since = None
    if prod_start_raw:
        try:
            since = datetime.strptime(prod_start_raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            since = None

    stmt = select(Signal).where(Signal.status != "OPEN")
    if since is not None:
        stmt = stmt.where(Signal.created_at >= since)

    async with SessionLocal() as session:
        signals = (await session.execute(stmt)).scalars().all()

    # Group by (side, timeframe, risk_level); win/loss is lifecycle-aware so a
    # TP-then-SL trade counts as a win, not a loss (matches the dashboard).
    groups: dict = defaultdict(list)
    for s in signals:
        groups[(s.side, s.timeframe, s.risk_level)].append(s)

    rows = []
    for (side, timeframe, risk_level), sigs in groups.items():
        if len(sigs) < 3:
            continue
        wins = sum(1 for s in sigs if winrate_bucket_for_signal(s) == BUCKET_WIN)
        losses = sum(1 for s in sigs if winrate_bucket_for_signal(s) == BUCKET_LOSS)
        rows.append(
            {
                "side": side,
                "timeframe": timeframe,
                "risk_level": risk_level,
                "total": len(sigs),
                "winrate": round(wins / max(wins + losses, 1) * 100, 1),
                "avg_conf": _avg([float(s.confidence) for s in sigs if s.confidence is not None]),
                "avg_rr": _avg([float(s.risk_reward) for s in sigs if s.risk_reward is not None]),
                "avg_pnl": _avg([float(s.pnl_pct) for s in sigs if s.pnl_pct is not None]),
            }
        )

    rows.sort(key=lambda r: (r["avg_pnl"] is not None, r["avg_pnl"]), reverse=True)

    print("\n🧠 ARGUS QUANT ADAPTIVE SCORING REPORT")
    print("=" * 70)

    if not rows:
        print("Not enough closed production signals yet.")
        return

    for r in rows:
        print(
            f"{r['side']} {r['timeframe']} {r['risk_level']} | "
            f"total={r['total']} winrate={r['winrate']}% "
            f"avg_conf={r['avg_conf']} avg_rr={r['avg_rr']} avg_pnl={r['avg_pnl']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
