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
import functools
import signal
from typing import Optional

import uvicorn
import uvloop

from app.config import settings, validate_startup
from app.dashboard import create_app
from app.database import init_db, repo, shutdown_db
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
    logger.info("  ARGUS QUANT — STARTUP DIAGNOSTICS")
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
        # ── Pre-persist duplicate guard (layer 2 of 3) ───────────────
        # Layer 1 is in the scanner.  This layer catches race conditions
        # where two concurrent scan tasks both passed layer 1 before either
        # was written to the DB.
        if settings.block_same_symbol_while_open:
            symbol = sig.get("symbol", "")
            if symbol and await repo.has_active_signal(symbol):
                logger.warning(
                    f"SKIP_DUPLICATE_ACTIVE_SIGNAL symbol={symbol} "
                    f"side={sig.get('side')} "
                    f"reason=existing_open_signal (pre-persist guard)"
                )
                return

        try:
            persisted = await repo.create_signal(
                {
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
                    "trend_score": sig.get("trend_score"),
                    "structure_score": sig.get("structure_score"),
                    "setup_score": sig.get("setup_score"),
                    "entry_score": sig.get("entry_score"),
                    # Sprint 16A — signal diagnostics
                    "diagnostics": sig.get("diagnostics"),
                    # Sprint 16C — dynamic RR method
                    "rr_method": sig.get("rr_method"),
                    # Sprint 19A — market regime at signal-creation time.
                    # Previously dropped here, so the column stayed NULL even
                    # though the value was present in diagnostics.
                    "market_regime": sig.get("market_regime"),
                    "regime_score": sig.get("regime_score"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"💾 persist signal FAILED {sig.get('symbol')}: {exc}")
            await self.bot.alert_admin("Persist Signal Failed", str(exc))
            return

        logger.info(
            f"💾 signal #{persisted.id} SAVED  "
            f"{sig['symbol']} {sig['side']} "
            f"conf={sig['confidence']}% rr=1:{sig['risk_reward']}"
        )

        # Tag the signal dict with the DB ID so the publisher guard can
        # use has_active_signal_excluding() and not block the new signal itself.
        sig["_signal_id"] = persisted.id

        # Open a paper position for every valid MTF signal (no real funds)
        try:
            from app.paper_follow.trading import open_paper_position

            await open_paper_position(persisted)
            logger.info(f"📊 paper position opened for signal #{persisted.id} {sig['symbol']}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"paper position open failed #{persisted.id}: {exc}")

        # Sprint 20D — demo auto-trading: open per-user paper positions for
        # opted-in users. Feature-flagged + isolated; never blocks broadcast.
        if settings.auto_trade_demo_enabled:
            try:
                from app.auto_engine.engine import on_new_signal

                await on_new_signal(persisted.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"auto-engine open failed #{persisted.id}: {exc}")

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
            logger.info(f"📤 signal #{persisted.id} BROADCAST to {len(sent_messages)} chat(s)")
        else:
            logger.warning(
                f"📤 signal #{persisted.id} NOT broadcast — "
                "check TELEGRAM_SIGNAL_CHAT_ID and tier routing config"
            )

    async def _handle_tracker_event(self, payload: dict) -> None:
        await self.bot.broadcast_event(payload)

        # Keep paper position in sync with tracker events (TP1/TP2/TP3/SL)
        event = payload.get("event", "")
        if event in ("TP1", "TP2", "TP3", "SL"):
            try:
                from app.paper_follow.trading import on_signal_event

                await on_signal_event(
                    signal_id=int(payload["signal_id"]),
                    event=event,
                    pnl_pct=float(payload.get("pnl_pct") or 0),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"paper position update failed ({event}): {exc}")

            # Sprint 20D — manage demo auto-trading positions (break-even /
            # trailing / close) for the same signal event.
            if settings.auto_trade_demo_enabled:
                try:
                    from app.auto_engine.engine import on_signal_event as auto_event

                    await auto_event(
                        signal_id=int(payload["signal_id"]),
                        event=event,
                        pnl_pct=float(payload.get("pnl_pct") or 0),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"auto-engine update failed ({event}): {exc}")

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

        # Load the admin runtime live-trading switch from system_settings (no-op
        # default OFF). live_gate_open() also requires MOCK_EXCHANGE_MODE=false.
        try:
            from app.exchange_adapters import live_gate_open, load_runtime_live_enabled

            await load_runtime_live_enabled()
            logger.info(f"  Live-trading gate: {'OPEN' if live_gate_open() else 'CLOSED'}")
        except Exception as exc:  # noqa: BLE001 — never block boot on this
            logger.warning(f"runtime live switch load skipped: {exc!r}")

        await universe.refresh()

        self.tracker.on_update(self._handle_tracker_event)

        self._tasks = [
            asyncio.create_task(run_forever(universe_loop, name="universe")),
            asyncio.create_task(run_forever(self.scanner.run_forever, name="scanner")),
            asyncio.create_task(run_forever(self.tracker.run_forever, name="tracker")),
            asyncio.create_task(run_forever(ws_price_loop, name="ws_price")),
            asyncio.create_task(self._run_dashboard()),
        ]

        # Sprint 21C — one-shot position-recovery sweep at boot (no-ops unless
        # POSITION_RECOVERY_ENABLED; never opens positions; never raises).
        try:
            from app.recovery import run_startup_recovery

            self._tasks.append(asyncio.create_task(run_startup_recovery()))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"startup recovery not scheduled (non-fatal): {exc!r}")

        # Periodic DB↔exchange reconciliation sweep (read-only; no-ops unless
        # RECONCILIATION_LOOP_ENABLED). The loop owns its own supervise/sleep
        # cycle, so it is scheduled directly (not via run_forever): when disabled
        # it returns immediately instead of busy-looping. Alerts admins on drift.
        try:
            from app.reconciliation.loop import reconciliation_loop

            self._tasks.append(asyncio.create_task(reconciliation_loop(alert=self.bot.alert_admin)))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"reconciliation loop not scheduled (non-fatal): {exc!r}")

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
            loop.add_signal_handler(sig, functools.partial(_handler, sig.name))
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
