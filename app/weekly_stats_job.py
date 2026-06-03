import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from telegram import Bot, constants

from app.config import settings
from app.database.models import Signal
from app.database.session import SessionLocal
from app.telegram_bot.stats_card import make_stats_card


async def main():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=7)

    async with SessionLocal() as session:
        result = await session.execute(select(Signal))
        signals = result.scalars().all()

    week_signals = [s for s in signals if s.created_at and s.created_at >= start]

    closed = [s for s in week_signals if s.status in ["TP1", "TP2", "TP3", "SL"]]
    wins = len([s for s in closed if s.status in ["TP1", "TP2", "TP3"]])
    losses = len([s for s in closed if s.status == "SL"])
    total = len(week_signals)

    winrate = (wins / max(1, wins + losses)) * 100
    pnl = sum(float(s.pnl_pct or 0) for s in closed)

    data = {
        "signals": total,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "pnl": pnl,
    }

    card = make_stats_card(data)

    caption = (
        "📊 <b>ARGUS QUANT</b>\n\n"
        "7D Performance\n"
        f"Signals • <code>{total}</code>\n"
        f"Wins • <code>{wins}</code> | Losses • <code>{losses}</code>\n"
        f"Winrate • <code>{winrate:.1f}%</code>\n"
        f"PnL • <code>{pnl:+.2f}%</code>\n\n"
        "⚡ Powered by Argus Quant AI"
    )

    bot = Bot(settings.telegram_bot_token)

    for chat_id in str(settings.telegram_signal_chat_id).split(","):
        chat_id = chat_id.strip()
        if not chat_id:
            continue

        with open(card, "rb") as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                parse_mode=constants.ParseMode.HTML,
            )


asyncio.run(main())
