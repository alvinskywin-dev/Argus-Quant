"""
Main entry point.

Boots PostgreSQL schema, starts:
    - symbol universe refresher
    - market scanner
    - signal tracker
    - Telegram bot
    - FastAPI dashboard

…all as supervised asyncio tasks. Handles graceful shutdown on SIGTERM/SIGINT.

Startup self-diagnostics verify all critical connections before serving.
"""
from __future__ import annotations

import asyncio
import signal
from typing import Optional

import uvicorn
import uvloop

from app.config import settings, validate_startup
from app.dashboard import create_app
from app.database import init_db, shutdown_db
from app.database import repo
from app.market_data import (
    shutdown_client,
    universe,
    universe_loop,
)
from app.market_data.binance_client import get_client
from app.market_data.cache import get_redis, shutdown_redis
from app.market_data.ws_engine import ws_price_loop
from app.scanner import MarketScanner, SignalTracker
from app.telegram_bot import TelegramBot
from app.utils.helpers import run_forever
from app.utils.logger import logger


# ---------- startup diagnostics ----------

async def _check_binance() -> bool:
    try:
        client = await get_client()
        info = await client.exchange_info()
        syms = [s for s in info.get("symbols", []) if s.get("status") == "TRADING"]
        logger.info(f"✅ Binance OK — {len(syms)} active symbols")
        return True
    except Exception as exc:
        logger.error(f"❌ Binance FAILED: {exc}")
        return False


async def _check_database() -> bool:
    try:
        await init_db()
        cnt = await repo.count_signals_since(
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            - __import__("datetime").timedelta(days=30)
        )
        logger.info(f"✅ Database OK — {cnt} signals in last 30d")
        return True
    except Exception as exc:
        logger.error(f"❌ Database FAILED: {exc}")
        return False


async def _check_redis() -> bool:
    try:
        r = await get_redis()
        await r.ping()
        logger.info("✅ Redis OK")
        return True
    except Exception as exc:
        logger.warning(f"⚠️  Redis FAILED (non-fatal, cache disabled): {exc}")
        return True  # non-fatal


async def _check_telegram(bot: TelegramBot) -> bool:
    if not settings.telegram_bot_token:
        logger.warning("⚠️  TELEGRAM_BOT_TOKEN not set — Telegram disabled")
        return True  # non-fatal, bot just won't send
    if bot.app is None:
        logger.warning("⚠️  Telegram app not initialized yet")
        return True
    try:
        me = await bot.app.bot.get_me()
        logger.info(f"✅ Telegram OK — @{me.username}")
        return True
    except Exception as exc:
        logger.error(f"❌ Telegram FAILED: {exc}")
        return False


async def _startup_report(bot: TelegramBot) -> None:
    """Print startup summary and fail loudly on critical errors."""
    logger.info("=" * 60)
    logger.info("  ALPHA RADAR SIGNALS — STARTUP DIAGNOSTICS")
    logger.info("=" * 60)

    binance_ok = await _check_binance()
    db_ok = await _check_database()
    redis_ok = await _check_redis()
    tg_ok = await _check_telegram(bot)

    logger.info("-" * 60)
    logger.info(f"  Binance:   {'OK' if binance_ok else 'FAILED'}")
    logger.info(f"  Database:  {'OK' if db_ok else 'FAILED'}")
    logger.info(f"  Redis:     {'OK' if redis_ok else 'DEGRADED (no cache)'}")
    logger.info(f"  Telegram:  {'OK' if tg_ok else 'FAILED'}")
    logger.info(f"  Timeframes: {', '.join(settings.timeframes)}")
    logger.info(f"  Scan interval: {settings.scan_interval_sec}s")
    logger.info(f"  Min confidence: {settings.min_confidence}%")
    logger.info(f"  Min RR: 1:{settings.min_rr}")
    logger.info(f"  Max signals/hr: {settings.max_signals_per_hour}")
    logger.info(f"  Dashboard port: {settings.dashboard_port}")
    logger.info("=" * 60)

    if not db_ok:
        raise RuntimeError("Database connection failed — cannot start")
    if not binance_ok:
        raise RuntimeError("Binance connection failed — cannot start")


# ---------- application ----------

class App:
    def __init__(self) -> None:
        self.bot = TelegramBot()
        self.tracker = SignalTracker(poll_sec=30)
        self.scanner = MarketScanner(on_signal=self._handle_signal, concurrency=12)
        self._tasks: list[asyncio.Task] = []
        self._dashboard_server: Optional[uvicorn.Server] = None
        self._stopping = asyncio.Event()

    async def _handle_signal(self, sig: dict) -> None:
        """Called by the scanner whenever a new signal is emitted."""
        try:
            persisted = await repo.create_signal({
                "symbol":         sig["symbol"],
                "side":           sig["side"],
                "timeframe":      sig["timeframe"],
                "confidence":     sig["confidence"],
                "risk_level":     sig["risk_level"],
                "strategy":       sig["strategy"],
                "reasons":        sig["reasons"],
                "entry_low":      sig["entry_low"],
                "entry_high":     sig["entry_high"],
                "tp1":            sig["tp1"],
                "tp2":            sig["tp2"],
                "tp3":            sig["tp3"],
                "stop_loss":      sig["stop_loss"],
                "risk_reward":    sig["risk_reward"],
                "status":         "OPEN",
                "trend_score":    sig.get("trend_score"),
                "structure_score":sig.get("structure_score"),
                "setup_score":    sig.get("setup_score"),
                "entry_score":    sig.get("entry_score"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"💾 persist signal FAILED {sig.get('symbol')}: {exc}")
            await self.bot.alert_admin("Persist Signal Failed", str(exc))
            return

        logger.info(
            f"💾 signal #{persisted.id} SAVED  "
            f"{sig['symbol']} {sig['side']} "
            f"conf={sig['confidence']}% rr=1:{sig['risk_reward']}"
        )

        try:
            sent_messages = await self.bot.broadcast_signal(sig)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"📤 broadcast FAILED #{persisted.id}: {exc}")
            await self.bot.alert_admin("Broadcast Signal Failed", str(exc))
            sent_messages = []

        if sent_messages:
            await repo.update_signal(
                persisted.id,
                {"telegram_message_id": sent_messages[0]["message_id"]},
            )
            for item in sent_messages:
                await repo.save_signal_message(
                    persisted.id,
                    item["chat_id"],
                    item["message_id"],
                )
            logger.info(
                f"📤 signal #{persisted.id} BROADCAST to {len(sent_messages)} chat(s)"
            )
        else:
            logger.warning(
                f"📤 signal #{persisted.id} NOT broadcast — "
                "check TELEGRAM_SIGNAL_CHAT_ID and tier routing config"
            )

    async def _handle_tracker_event(self, payload: dict) -> None:
        await self.bot.broadcast_event(payload)

    async def _run_dashboard(self) -> None:
        cfg = uvicorn.Config(
            create_app(),
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level=settings.log_level.lower(),
            access_log=False,
            loop="uvloop",
        )
        self._dashboard_server = uvicorn.Server(cfg)
        await self._dashboard_server.serve()

    async def start(self) -> None:
        logger.info("=== AI Futures Signal System booting ===")

        await self.bot.start()
        await _startup_report(self.bot)
        await universe.refresh()

        self.tracker.on_update(self._handle_tracker_event)

        self._tasks = [
            asyncio.create_task(run_forever(universe_loop, name="universe")),
            asyncio.create_task(run_forever(self.scanner.run_forever, name="scanner")),
            asyncio.create_task(run_forever(self.tracker.run_forever, name="tracker")),
            asyncio.create_task(run_forever(ws_price_loop, name="ws_price")),
            asyncio.create_task(self._run_dashboard()),
        ]

        logger.info(
            f"=== all services running  "
            f"universe={len(universe.symbols)} symbols  "
            f"dashboard=:{settings.dashboard_port} ==="
        )
        await self._stopping.wait()

    async def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        logger.info("=== shutting down ===")

        if self._dashboard_server is not None:
            self._dashboard_server.should_exit = True

        for t in self._tasks:
            t.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.sleep(0.5)

        await self.bot.shutdown()
        await shutdown_client()
        await shutdown_redis()
        await shutdown_db()

        logger.info("=== bye ===")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, app: App) -> None:
    def _handler(sig_name: str) -> None:
        logger.info(f"received {sig_name}")
        asyncio.create_task(app.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _handler(s.name))
        except NotImplementedError:
            pass  # Windows


async def _amain() -> None:
    app = App()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, app)
    try:
        await app.start()
    except asyncio.CancelledError:
        pass
    finally:
        await app.stop()


def main() -> None:
    validate_startup(settings)
    uvloop.install()
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
