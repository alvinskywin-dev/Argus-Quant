"""
The main scanner loop.

Pipeline per symbol:
    1. fetch klines for each configured timeframe (cached)
    2. build feature snapshots
    3. score each TF independently (LONG vs SHORT — pick best per TF)
    4. aggregate across TFs (confluence)
    5. apply smart filters + cooldown + rate limit
    6. build trade levels (entry/TP/SL)
    7. persist signal + emit via Telegram

Concurrency:
    - Symbols processed with a bounded asyncio semaphore (CPU bound math
      is light; the bottleneck is the REST API → throttled in the client).
    - One scan cycle every `SCAN_INTERVAL_SEC`.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Optional

from app.ai_scoring import MTFDecision, Score, aggregate, score_side
from app.config import settings
from app.market_data import fetch_klines, universe
from app.risk import build_levels, cooldown, passes_market_filters, rate_limiter
from app.strategies import FeatureSnapshot, build_snapshot
from app.utils.helpers import utcnow
from app.utils.logger import logger


SignalCallback = Callable[[dict], Awaitable[None]]


class MarketScanner:
    def __init__(self, on_signal: SignalCallback, concurrency: int = 12) -> None:
        self.on_signal = on_signal
        self.sem = asyncio.Semaphore(concurrency)
        self.timeframes: List[str] = settings.timeframes or ["15m", "1h"]
        self.primary_tf: str = self.timeframes[0]

    async def _snap_for(self, symbol: str, tf: str) -> Optional[FeatureSnapshot]:
        df = await fetch_klines(symbol, tf, limit=250)
        if df.empty:
            return None
        return build_snapshot(symbol, tf, df)

    async def _analyze_symbol(self, symbol: str) -> Optional[dict]:
        async with self.sem:
            snaps: Dict[str, FeatureSnapshot] = {}
            for tf in self.timeframes:
                s = await self._snap_for(symbol, tf)
                if s is not None:
                    snaps[tf] = s
            if not snaps or self.primary_tf not in snaps:
                return None

            # Score each TF separately (best side)
            scores: Dict[str, Score] = {}
            for tf, snap in snaps.items():
                long_s = score_side(snap, "LONG")
                short_s = score_side(snap, "SHORT")
                scores[tf] = long_s if long_s.confidence >= short_s.confidence else short_s

            decision: Optional[MTFDecision] = aggregate(scores, self.primary_tf)
            if not decision:
                return None

            primary_snap = snaps[self.primary_tf]
            ok, reason = passes_market_filters(primary_snap, decision)
            if not ok:
                logger.debug(f"{symbol} {decision.side} {decision.confidence}% filtered: {reason}")
                return None

            levels = build_levels(primary_snap, decision.side)
            if levels is None:
                return None

            if not await cooldown.can_emit(symbol, decision.side):
                return None
            if not await rate_limiter.acquire():
                logger.info("hourly rate limit reached — skipping new emission")
                return None

            await cooldown.mark_emitted(symbol, decision.side)

            signal = {
                "symbol": symbol,
                "side": decision.side,
                "timeframe": self.primary_tf,
                "confidence": decision.confidence,
                "risk_level": decision.risk_level,
                "strategy": "MTF_SMC_MOMENTUM",
                "reasons": " | ".join(decision.reasons[:8]),
                "entry_low": levels.entry_low,
                "entry_high": levels.entry_high,
                "tp1": levels.tp1,
                "tp2": levels.tp2,
                "tp3": levels.tp3,
                "stop_loss": levels.stop_loss,
                "risk_reward": levels.risk_reward,
                "status": "OPEN",
                "_snapshot": primary_snap,        # available to consumers
                "_contributing_tfs": decision.contributing_tfs,
                "_detected_at": utcnow(),
            }
            return signal

    async def scan_once(self) -> int:
        symbols = list(universe.symbols)
        if not symbols:
            logger.warning("symbol universe empty — refresh first")
            return 0

        tasks = [self._analyze_symbol(s) for s in symbols]
        emitted = 0
        for fut in asyncio.as_completed(tasks):
            try:
                result = await fut
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"analyzer task failed: {exc}")
                continue
            if result:
                try:
                    await self.on_signal(result)
                    emitted += 1
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"on_signal callback failed: {exc}")
        logger.info(f"scan cycle complete — analyzed={len(symbols)} emitted={emitted}")
        return emitted

    async def run_forever(self) -> None:
        # Ensure universe is loaded once before first scan
        if not universe.symbols:
            await universe.refresh()

        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"scan cycle error: {exc}")
            await asyncio.sleep(settings.scan_interval_sec)
