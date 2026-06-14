import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from telegram import Bot, constants

from app.analytics.trade_outcome import BUCKET_LOSS, BUCKET_WIN, winrate_bucket_for_signal
from app.config import settings
from app.database.models import Signal
from app.database.session import SessionLocal


async def build_report() -> str:
    since = datetime.now(timezone.utc) - timedelta(days=1)

    async with SessionLocal() as session:
        result = await session.execute(select(Signal).where(Signal.created_at >= since))
        signals = result.scalars().all()

    total = len(signals)
    # Lifecycle-aware: a TP-then-SL trade counts as a win, not a loss.
    wins = len([s for s in signals if winrate_bucket_for_signal(s) == BUCKET_WIN])
    losses = len([s for s in signals if winrate_bucket_for_signal(s) == BUCKET_LOSS])
    pnls = [float(s.pnl_pct) for s in signals if s.pnl_pct is not None]
    avg_pnl = round(sum(pnls) / len(pnls), 2) if pnls else 0
    winrate = round((wins / max(wins + losses, 1)) * 100, 1)

    return f"""
📊 <b>ARGUS QUANT DAILY REPORT</b>

Signals • <code>{total}</code>
Wins • <code>{wins}</code>
Losses • <code>{losses}</code>

Winrate • <code>{winrate}%</code>
Avg PnL • <code>{avg_pnl}%</code>

⚡ <b>ARGUS QUANT</b>
"""


async def main():
    text_msg = await build_report()
    print(text_msg)

    bot = Bot(settings.telegram_bot_token)

    targets = []
    vip = os.getenv("VIP_CHAT_ID", "").strip()
    if vip:
        targets.append(vip)

    for admin_id in settings.admin_ids:
        targets.append(str(admin_id))

    for chat_id in dict.fromkeys(targets):
        await bot.send_message(
            chat_id=chat_id,
            text=text_msg,
            parse_mode=constants.ParseMode.HTML,
        )


if __name__ == "__main__":
    asyncio.run(main())
