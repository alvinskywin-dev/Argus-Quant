import asyncio
from telegram import Bot, constants

from app.config import settings
from app.market_data.cache import get_redis, shutdown_redis


async def main():
    ok = True
    errors = []

    try:
        redis = await get_redis()
        await redis.ping()
    except Exception as e:
        ok = False
        errors.append(f"Redis: {e}")
    finally:
        await shutdown_redis()

    if not ok:
        bot = Bot(settings.telegram_bot_token)
        text = "🚨 <b>ALPHA RADAR UPTIME ALERT</b>\n\n" + "\n".join(
            f"<code>{x}</code>" for x in errors
        )

        for admin_id in settings.admin_ids:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=constants.ParseMode.HTML,
            )

    print("OK" if ok else "FAILED")


if __name__ == "__main__":
    asyncio.run(main())
