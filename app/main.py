"""
Main entry point.

Boots PostgreSQL schema, starts:
    - symbol universe refresher
    - market scanner
    - signal tracker
    - Telegram bot
    - FastAPI dashboard

…all as supervised asyncio tasks. Handles graceful shutdown on SIGTERM/SIGINT.
"""
from __future__ import annotations

import asyncio
import signal
from typing import Optional

import uvicorn
import uvloop

from app.config import settings
from app.dashboard import create_app
from app.database import init_db, shutdown_db
from app.database import repo
from app.market_data import (
    shutdown_client,
    universe,
    universe_loop,
)
from app.market_data.cache import shutdown_redis
from app.market_data.ws_engine import ws_price_loop
from app.scanner import MarketScanner, SignalTracker
from app.telegram_bot import TelegramBot
from app.utils.helpers import run_forever
from app.utils.logger import logger


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
        # Persist first — gets the id back
        db_fields = {k: v for k, v in sig.items() if not k.startswith("_")}
        # Strip non-column fields
        db_fields.pop("status", None)
        db_fields["status"] = "OPEN"
        try:
            persisted = await repo.create_signal({
                "symbol": sig["symbol"],
                "side": sig["side"],
                "timeframe": sig["timeframe"],
                "confidence": sig["confidence"],
                "risk_level": sig["risk_level"],
                "strategy": sig["strategy"],
                "reasons": sig["reasons"],
                "entry_low": sig["entry_low"],
                "entry_high": sig["entry_high"],
                "tp1": sig["tp1"],
                "tp2": sig["tp2"],
                "tp3": sig["tp3"],
                "stop_loss": sig["stop_loss"],
                "risk_reward": sig["risk_reward"],
                "status": "OPEN",
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"persist signal failed: {exc}")
            await self.bot.alert_admin("Persist Signal Failed", str(exc))
            return

        try:
            sent_messages = await self.bot.broadcast_signal(sig)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"broadcast signal failed: {exc}")
            await self.bot.alert_admin("Broadcast Signal Failed", str(exc))
            sent_messages = []

        if sent_messages:
            # keep legacy first message id for backward compatibility
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
            f"🚀 SIGNAL #{persisted.id} {sig['symbol']} {sig['side']} "
            f"conf={sig['confidence']}% rr=1:{sig['risk_reward']}"
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
        await init_db()
        await universe.refresh()

        self.tracker.on_update(self._handle_tracker_event)

        await self.bot.start()

        # Run everything as supervised forever-tasks
        self._tasks = [
            asyncio.create_task(run_forever(universe_loop, name="universe")),
            asyncio.create_task(run_forever(self.scanner.run_forever, name="scanner")),
            asyncio.create_task(run_forever(self.tracker.run_forever, name="tracker")),
            asyncio.create_task(run_forever(ws_price_loop, name="ws_price")),
            asyncio.create_task(self._run_dashboard()),
        ]
        logger.info("=== all services running ===")
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

        # allow pending aiohttp requests to settle
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
    uvloop.install()
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
