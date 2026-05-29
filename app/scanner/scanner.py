"""
Main scanner loop — strict Multi-Timeframe pipeline.

Per-symbol pipeline:
    1D  →  Trend Filter
    4H  →  Market Structure
    1H  →  Setup Detection
    15M →  Entry Trigger
    RR check  →  min 2.0
    Cooldown  →  30 min same direction (opposite always allowed)
    Rate limit→  hourly cap

Rejection diagnostics are printed for every filtered symbol.
A full scan summary is printed at the end of every cycle.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Optional

from app.ai_scoring import MTFDecision, MTFRejection, evaluate_pipeline
from app.config import settings
from app.market_data import fetch_klines, universe
from app.risk import build_levels, cooldown, passes_market_filters, rate_limiter
from app.strategies import FeatureSnapshot, build_snapshot
from app.utils.helpers import utcnow
from app.utils.logger import logger

SignalCallback = Callable[[dict], Awaitable[None]]

# Pipeline timeframes — order must match MTF stages: entry→trend
MTF_TIMEFRAMES = ("1d", "4h", "1h", "15m")


class MarketScanner:
    def __init__(self, on_signal: SignalCallback, concurrency: int = 12) -> None:
        self.on_signal = on_signal
        self.sem = asyncio.Semaphore(concurrency)
        # Keep for startup report compatibility
        self.timeframes: List[str] = list(MTF_TIMEFRAMES)
        self.primary_tf: str = "15m"

    async def _snap_for(self, symbol: str, tf: str) -> Optional[FeatureSnapshot]:
        df = await fetch_klines(symbol, tf, limit=250)
        if df.empty:
            return None
        return build_snapshot(symbol, tf, df)

    async def _analyze_symbol(self, symbol: str) -> Optional[dict]:
        """
        Returns:
            signal dict       — new valid signal
            {"_DIAG_": True}  — rejection with stage / detail metadata
            None              — skipped silently
        """
        async with self.sem:
            snaps: Dict[str, FeatureSnapshot] = {}
            for tf in MTF_TIMEFRAMES:
                s = await self._snap_for(symbol, tf)
                if s is not None:
                    snaps[tf] = s

            # ── MTF pipeline ────────────────────────────────────────────
            decision, rejection = evaluate_pipeline(snaps)

            if rejection is not None:
                stage = rejection.stage
                side  = rejection.side

                if stage == "no_data":
                    logger.debug(f"⏭ {symbol} skipped: {rejection.detail}")
                    return {"_DIAG_": True, "stage": "no_data"}

                stage_label = {
                    "trend":     "Trend failed",
                    "structure": "Structure failed",
                    "setup":     "Setup failed",
                    "entry":     "Entry failed",
                }.get(stage, f"{stage.capitalize()} failed")

                logger.info(
                    f"⛔ {symbol} {side} — {stage_label}: {rejection.detail}"
                )
                return {"_DIAG_": True, "stage": stage, "side": side}

            # ── Market quality filters (confidence adjustments + hard rejects) ─
            primary_snap = snaps["15m"]

            ok, reason = passes_market_filters(primary_snap, decision)
            if not ok:
                logger.info(
                    f"⛔ {symbol} {decision.side} — Confidence failed: "
                    f"conf={decision.confidence:.1f}% (need>={settings.min_confidence}) "
                    f"filter={reason}"
                )
                return {
                    "_DIAG_": True, "stage": "confidence",
                    "side": decision.side, "conf": decision.confidence,
                }

            # ── RR check ────────────────────────────────────────────────
            levels = build_levels(primary_snap, decision.side)
            if levels is None:
                logger.info(
                    f"⛔ {symbol} {decision.side} — RR failed: "
                    f"build_levels returned None (min_rr={settings.min_rr})"
                )
                return {"_DIAG_": True, "stage": "rr", "side": decision.side}

            if levels.risk_reward < settings.min_rr:
                logger.info(
                    f"⛔ {symbol} {decision.side} — RR failed: "
                    f"rr={levels.risk_reward:.2f} < {settings.min_rr}"
                )
                return {
                    "_DIAG_": True, "stage": "rr",
                    "side": decision.side, "rr": levels.risk_reward,
                }

            # ── Cooldown (same direction only) ───────────────────────────
            if not await cooldown.can_emit(symbol, decision.side):
                logger.info(
                    f"⛔ {symbol} {decision.side} — Cooldown failed: "
                    f"same-direction cooldown active ({settings.symbol_cooldown_minutes}m)"
                )
                return {"_DIAG_": True, "stage": "cooldown", "side": decision.side}

            # ── Hourly rate cap ──────────────────────────────────────────
            if not await rate_limiter.acquire():
                used = await rate_limiter.used()
                logger.info(
                    f"⛔ {symbol} {decision.side} — Rate limit: "
                    f"{used}/{settings.max_signals_per_hour} signals this hour"
                )
                return {"_DIAG_": True, "stage": "rate_limit", "side": decision.side}

            await cooldown.mark_emitted(symbol, decision.side)

            logger.info(
                f"✅ SIGNAL  {symbol} {decision.side}  "
                f"tier={decision.tier}  conf={decision.confidence:.1f}%  "
                f"rr=1:{levels.risk_reward}  tfs={decision.contributing_tfs}"
            )

            return {
                "symbol":      symbol,
                "side":        decision.side,
                "timeframe":   "15m",
                "confidence":  decision.confidence,
                "risk_level":  decision.risk_level,
                "strategy":    "MTF_SMC_STRICT",
                "reasons":     " | ".join(decision.reasons[:8]),
                "entry_low":   levels.entry_low,
                "entry_high":  levels.entry_high,
                "tp1":         levels.tp1,
                "tp2":         levels.tp2,
                "tp3":         levels.tp3,
                "stop_loss":   levels.stop_loss,
                "risk_reward": levels.risk_reward,
                "status":      "OPEN",
                "_snapshot":         primary_snap,
                "_contributing_tfs": decision.contributing_tfs,
                "_detected_at":      utcnow(),
                "_tier":             decision.tier,
            }

    async def scan_once(self) -> int:
        symbols = list(universe.symbols)
        if not symbols:
            logger.warning("symbol universe empty — refresh first")
            return 0

        tasks = [self._analyze_symbol(s) for s in symbols]
        emitted = 0

        counters: Dict[str, int] = {
            "analyzed":   len(symbols),
            "no_data":    0,
            "trend":      0,
            "structure":  0,
            "setup":      0,
            "entry":      0,
            "confidence": 0,
            "rr":         0,
            "cooldown":   0,
            "rate_limit": 0,
            "error":      0,
        }

        for fut in asyncio.as_completed(tasks):
            try:
                result = await fut
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"analyzer task failed: {exc}")
                counters["error"] += 1
                continue

            if not result:
                continue
            if result.get("_DIAG_"):
                stage = result.get("stage", "error")
                counters[stage] = counters.get(stage, 0) + 1
                continue
            try:
                await self.on_signal(result)
                emitted += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"on_signal callback failed: {exc}")

        # ── End-of-scan summary ─────────────────────────────────────────
        c = counters
        trend_pass     = c["analyzed"] - c["no_data"] - c["trend"]
        structure_pass = trend_pass    - c["structure"]
        setup_pass     = structure_pass - c["setup"]
        entry_pass     = setup_pass    - c["entry"]
        conf_pass      = entry_pass    - c["confidence"]
        rr_pass        = conf_pass     - c["rr"]

        logger.info(
            f"\n{'─'*52}\n"
            f"  📊 SCAN SUMMARY\n"
            f"  Analyzed:        {c['analyzed']}\n"
            f"  Trend pass:      {trend_pass}\n"
            f"  Structure pass:  {structure_pass}\n"
            f"  Setup pass:      {setup_pass}\n"
            f"  Entry pass:      {entry_pass}\n"
            f"  Confidence pass: {conf_pass}\n"
            f"  RR pass:         {rr_pass}\n"
            f"  Emitted:         {emitted}\n"
            f"{'─'*52}"
        )

        return emitted

    async def run_forever(self) -> None:
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
