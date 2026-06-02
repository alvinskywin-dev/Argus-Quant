import asyncio
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from telegram import Bot, constants

from app.config import settings
from app.database.session import SessionLocal


async def build_report() -> str:
    since = datetime.now(timezone.utc) - timedelta(days=1)

    async with SessionLocal() as session:
        r = await session.execute(
            text("""
                SELECT
                    COUNT(*) total,
                    SUM(CASE WHEN status IN ('TP1','TP2','TP3') THEN 1 ELSE 0 END) wins,
                    SUM(CASE WHEN status='SL' THEN 1 ELSE 0 END) losses,
                    ROUND(AVG(pnl_pct)::numeric, 2) avg_pnl
                FROM signals
                WHERE created_at >= :since
            """),
            {"since": since},
        )
        row = r.first()

    total = row.total or 0
    wins = row.wins or 0
    losses = row.losses or 0
    avg_pnl = row.avg_pnl or 0
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
