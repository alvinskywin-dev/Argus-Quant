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

Sprint 16A: diagnostics object built and attached to each signal.
Sprint 16C: dynamic RR selection (atr / structure / liquidity).
Sprint 17:  optional liquidity sweep engine (ENABLE_LIQUIDITY_ENGINE=true).
Sprint 18:  adaptive threshold cycle runs after every full scan.
Sprint 19A: market regime engine — classifies BULL/BEAR/SIDEWAYS/HV/LV.
Sprint 19B: short protection layer — rejects low-quality SHORT signals.
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import pandas as pd

from app.ai_scoring import evaluate_pipeline
from app.config import settings
from app.database.models import OpenInterestSnapshot
from app.database.repo import has_active_signal, in_post_close_cooldown
from app.database.session import SessionLocal
from app.market_data import fetch_klines, universe
from app.market_data.funding import fetch_funding_rate, score_funding_for_side
from app.market_data.market_regime import ensure_regime_fresh, get_market_regime
from app.market_data.open_interest import fetch_oi_snapshot
from app.risk import build_levels, cooldown, passes_market_filters, rate_limiter
from app.scanner.short_protection import apply_short_protection
from app.strategies import FeatureSnapshot, build_snapshot
from app.utils.helpers import utcnow
from app.utils.logger import logger

SignalCallback = Callable[[dict], Awaitable[None]]

# Pipeline timeframes — order must match MTF stages: entry→trend
MTF_TIMEFRAMES = ("1d", "4h", "1h", "15m")


def _fmt_entry_diag(
    symbol: str,
    side: str,
    trend_score: float,
    struct_score: float,
    setup_score: float,
    entry_score: float,
    factors: dict,
    outcome: str,
) -> str:
    """Format a one-line setup diagnostic for any signal that reached entry evaluation."""
    factor_str = " ".join(
        f"{k.split()[0][0:3].upper()}={'✓' if v else '✗'}"
        for k, v in factors.items()
    )
    return (
        f"🔍 DIAG {symbol:>12} {side:<5} | "
        f"trend={trend_score:>4.1f} struct={struct_score:.0f}/5 "
        f"setup={setup_score:.0f}/5 entry={entry_score:.0f}/5 "
        f"[{factor_str}] → {outcome}"
    )


class MarketScanner:
    def __init__(self, on_signal: SignalCallback, concurrency: int = 12) -> None:
        self.on_signal = on_signal
        self.sem = asyncio.Semaphore(concurrency)
        self.timeframes: List[str] = list(MTF_TIMEFRAMES)
        self.primary_tf: str = "15m"

    async def _snap_for(
        self, symbol: str, tf: str
    ) -> Tuple[Optional[FeatureSnapshot], Optional[pd.DataFrame]]:
        """Fetch klines and build a FeatureSnapshot. Returns (snap, raw_df)."""
        df = await fetch_klines(symbol, tf, limit=250)
        if df.empty:
            return None, None
        return build_snapshot(symbol, tf, df), df

    async def _analyze_symbol(self, symbol: str) -> Optional[dict]:
        """
        Returns:
            signal dict       — new valid signal
            {"_DIAG_": True}  — rejection with stage / detail metadata
            None              — skipped silently
        """
        async with self.sem:
            snaps: Dict[str, FeatureSnapshot] = {}
            dfs: Dict[str, pd.DataFrame] = {}
            for tf in MTF_TIMEFRAMES:
                snap, df = await self._snap_for(symbol, tf)
                if snap is not None:
                    snaps[tf] = snap
                    if df is not None:
                        dfs[tf] = df

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

                if stage == "entry" and rejection.entry_factors:
                    logger.info(
                        _fmt_entry_diag(
                            symbol, side,
                            rejection.trend_score,
                            rejection.structure_score,
                            rejection.setup_score,
                            rejection.entry_score_pts,
                            rejection.entry_factors,
                            f"REJECTED@entry ({rejection.entry_score_pts:.0f}<{settings.entry_pass_score})",
                        )
                    )
                elif settings.log_rejection_detail:
                    logger.info(
                        f"⛔ {symbol} {side} — {stage_label}: {rejection.detail}"
                    )
                return {"_DIAG_": True, "stage": stage, "side": side}

            # ── Market quality filters ───────────────────────────────────
            primary_snap = snaps["15m"]

            logger.info(
                _fmt_entry_diag(
                    symbol, decision.side,
                    decision.trend_score,
                    decision.structure_score,
                    decision.setup_score,
                    decision.entry_score_pts,
                    decision.entry_factors,
                    f"entry_ok ({decision.entry_score_pts:.0f}/{settings.entry_pass_score})",
                )
            )

            ok, reason = passes_market_filters(primary_snap, decision)
            if not ok:
                if settings.log_rejection_detail:
                    logger.info(
                        f"⛔ {symbol} {decision.side} — Confidence failed: "
                        f"conf={decision.confidence:.1f}% (need>={settings.min_confidence}) "
                        f"filter={reason}"
                    )
                return {
                    "_DIAG_": True, "stage": "confidence",
                    "side": decision.side, "conf": decision.confidence,
                }

            # ── Sprint 17: Liquidity Sweep Engine (optional) ─────────────
            liq_signal = None
            liquidity_score = 0
            if settings.enable_liquidity_engine and "15m" in dfs:
                try:
                    from app.indicators.liquidity import (
                        analyze_liquidity,
                        liquidity_score_for_side,
                    )
                    liq_signal = analyze_liquidity(dfs["15m"])
                    liquidity_score = liquidity_score_for_side(liq_signal, decision.side)
                    if liquidity_score > 0:
                        logger.info(
                            f"💧 {symbol} {decision.side} | "
                            f"liquidity_score={liquidity_score} "
                            f"(sweep_up={liq_signal.sweep_up} sweep_down={liq_signal.sweep_down} "
                            f"eq_h={liq_signal.equal_highs} eq_l={liq_signal.equal_lows})"
                        )
                except Exception as exc:
                    logger.debug(f"liquidity engine failed for {symbol}: {exc}")

            # ── Sprint 16C: Dynamic RR ────────────────────────────────────
            levels = build_levels(primary_snap, decision.side, liq_signal, df_1d=dfs.get("1d"))
            if levels is None:
                if settings.log_rejection_detail:
                    logger.info(
                        f"⛔ {symbol} {decision.side} — RR failed: "
                        f"build_levels returned None (min_rr={settings.min_rr})"
                    )
                return {"_DIAG_": True, "stage": "rr", "side": decision.side}

            if levels.risk_reward < settings.min_rr:
                if settings.log_rejection_detail:
                    logger.info(
                        f"⛔ {symbol} {decision.side} — RR failed: "
                        f"rr={levels.risk_reward:.2f} < {settings.min_rr}"
                    )
                return {
                    "_DIAG_": True, "stage": "rr",
                    "side": decision.side, "rr": levels.risk_reward,
                }

            # ── Active-signal guard ──────────────────────────────────────
            if settings.block_same_symbol_while_open:
                if await has_active_signal(symbol):
                    logger.info(
                        f"SKIP_DUPLICATE_ACTIVE_SIGNAL symbol={symbol} "
                        f"side={decision.side} reason=existing_open_signal"
                    )
                    return {
                        "_DIAG_": True,
                        "stage": "duplicate_active",
                        "side": decision.side,
                    }
            elif settings.block_same_symbol_side_while_open:
                if await has_active_signal(symbol, side=decision.side):
                    logger.info(
                        f"SKIP_DUPLICATE_ACTIVE_SIGNAL symbol={symbol} "
                        f"side={decision.side} reason=existing_open_signal_same_side"
                    )
                    return {
                        "_DIAG_": True,
                        "stage": "duplicate_active",
                        "side": decision.side,
                    }

            # ── Post-close cooldown ──────────────────────────────────────
            if settings.signal_duplicate_cooldown_hours > 0:
                if await in_post_close_cooldown(
                    symbol, decision.side, settings.signal_duplicate_cooldown_hours
                ):
                    logger.info(
                        f"SKIP_DUPLICATE_ACTIVE_SIGNAL symbol={symbol} "
                        f"side={decision.side} "
                        f"reason=post_close_cooldown_{settings.signal_duplicate_cooldown_hours}h"
                    )
                    return {
                        "_DIAG_": True,
                        "stage": "post_close_cooldown",
                        "side": decision.side,
                    }

            # ── Cooldown (same direction only) ───────────────────────────
            if not await cooldown.can_emit(symbol, decision.side):
                if settings.log_rejection_detail:
                    logger.info(
                        f"⛔ {symbol} {decision.side} — Cooldown failed: "
                        f"same-direction cooldown active ({settings.symbol_cooldown_minutes}m)"
                    )
                return {"_DIAG_": True, "stage": "cooldown", "side": decision.side}

            # ── Hourly rate cap ──────────────────────────────────────────
            if not await rate_limiter.acquire():
                used = await rate_limiter.used()
                if settings.log_rejection_detail:
                    logger.info(
                        f"⛔ {symbol} {decision.side} — Rate limit: "
                        f"{used}/{settings.max_signals_per_hour} signals this hour"
                    )
                return {"_DIAG_": True, "stage": "rate_limit", "side": decision.side}

            await cooldown.mark_emitted(symbol, decision.side)

            # ── Open Interest scoring ────────────────────────────────────
            oi_snap = await fetch_oi_snapshot(
                symbol,
                primary_snap.price_change_pct,
                decision.side,
            )
            oi_score = oi_snap.oi_score if oi_snap else 0
            oi_diag = ""
            if oi_snap:
                oi_diag = (
                    f"OI: price_change={oi_snap.price_change_pct:+.2f}% "
                    f"oi_change={oi_snap.oi_change_15m:+.2f}% "
                    f"score={oi_score:+d}"
                )
                logger.info(f"📊 {symbol} {decision.side} | {oi_diag}")
                try:
                    async with SessionLocal() as db:
                        db.add(OpenInterestSnapshot(
                            symbol=symbol,
                            open_interest=oi_snap.open_interest,
                            oi_change_5m=oi_snap.oi_change_5m,
                            oi_change_15m=oi_snap.oi_change_15m,
                            oi_change_1h=oi_snap.oi_change_1h,
                            price_change_pct=oi_snap.price_change_pct,
                            oi_score=oi_score,
                        ))
                        await db.commit()
                except Exception as exc:
                    logger.debug(f"OI DB save failed for {symbol}: {exc}")

            # ── Funding Rate scoring ─────────────────────────────────────
            funding_data = None
            funding_fs = None
            funding_diag = ""
            if settings.funding_enabled:
                funding_data = await fetch_funding_rate(symbol)
                if funding_data:
                    funding_fs = score_funding_for_side(
                        funding_data.classification, decision.side
                    )
                    funding_diag = (
                        f"Funding: rate={funding_data.funding_rate * 100:.4f}% "
                        f"class={funding_data.classification} "
                        f"score={funding_fs.score:+d}"
                    )
                    logger.info(
                        f"💰 {symbol} {decision.side} | {funding_diag} | {funding_fs.reason}"
                    )
                    try:
                        from app.database.repo import save_funding_snapshot
                        await save_funding_snapshot(
                            symbol=symbol,
                            funding_rate=funding_data.funding_rate,
                            classification=funding_data.classification,
                            funding_time=funding_data.funding_time,
                            next_funding_time=funding_data.next_funding_time,
                        )
                    except Exception as exc:
                        logger.debug(f"funding DB save failed for {symbol}: {exc}")

            funding_score = funding_fs.score if funding_fs else 0
            funding_class = funding_data.classification if funding_data else None

            # ── Base confidence after OI, funding, and liquidity ─────────
            adjusted_confidence = round(
                max(0.0, min(100.0, decision.confidence + oi_score + funding_score + (liquidity_score * 0.5))),
                1,
            )

            # ── Sprint 19A: Market regime scoring impact ──────────────────
            regime = await get_market_regime()
            regime_delta = 0
            if regime:
                rn = regime.market_regime
                if rn == "BULL":
                    regime_delta = 5 if decision.side == "LONG" else -10
                elif rn == "BEAR":
                    regime_delta = 5 if decision.side == "SHORT" else -10
                elif rn == "SIDEWAYS":
                    regime_delta = -5
                # HIGH_VOLATILITY / LOW_VOLATILITY have no direct delta — handled below

                if regime_delta:
                    adjusted_confidence = round(
                        max(0.0, min(100.0, adjusted_confidence + regime_delta)),
                        1,
                    )

            # Secondary confidence gate: regime may have pushed us below threshold
            if adjusted_confidence < settings.min_confidence:
                if settings.log_rejection_detail:
                    logger.info(
                        f"⛔ {symbol} {decision.side} — Regime confidence gate: "
                        f"conf={adjusted_confidence:.1f}% after regime_delta={regime_delta:+d}"
                    )
                return {
                    "_DIAG_": True, "stage": "confidence",
                    "side": decision.side, "conf": adjusted_confidence,
                }

            # ── Sprint 19B: Short protection layer ───────────────────────
            short_protection = await apply_short_protection(
                side=decision.side,
                snaps=snaps,
                regime=regime,
                funding_class=funding_class,
                oi_snap=oi_snap,
                liquidity_score=liquidity_score,
                adjusted_confidence=adjusted_confidence,
            )
            if not short_protection.passed:
                logger.info(
                    f"🛡 SHORT REJECTED {symbol} — {short_protection.rejection_reason}"
                )
                return {
                    "_DIAG_": True, "stage": "short_protection",
                    "side": decision.side,
                    "reason": short_protection.rejection_reason,
                }

            logger.info(
                f"✅ SIGNAL  {symbol} {decision.side}  "
                f"tier={decision.tier}  conf={adjusted_confidence:.1f}%  "
                f"(base={decision.confidence:.1f} oi={oi_score:+d} "
                f"funding={funding_score:+d} liq={liquidity_score:+d} "
                f"regime={regime_delta:+d})  "
                f"rr=1:{levels.risk_reward} method={levels.rr_method}  "
                f"tfs={decision.contributing_tfs}"
            )

            # ── Sprint 16A / 19A: Build diagnostics object ───────────────
            diagnostics = json.dumps({
                "trend_score":      round(float(decision.trend_score), 1),
                "structure_score":  round(float(decision.structure_score), 1),
                "setup_score":      round(float(decision.setup_score), 1),
                "entry_score":      round(float(decision.entry_score_pts), 1),
                "funding_score":    funding_score,
                "oi_score":         oi_score,
                "liquidity_score":  liquidity_score,
                "base_confidence":  round(float(decision.confidence), 1),
                "total_score":      adjusted_confidence,
                "rr_method":        levels.rr_method,
                "funding_class":    funding_class,
                "tier":             decision.tier,
                # Sprint 19A
                "market_regime":    regime.market_regime if regime else None,
                "regime_score":     regime.regime_score if regime else None,
                "regime_delta":     regime_delta,
                # Sprint 19B
                "short_protection_pass":    short_protection.passed,
                "short_rejection_reason":   short_protection.rejection_reason,
                # Stop-Loss Engine V2 — SL placement diagnostics
                **levels.sl_diag,
            })

            return {
                "symbol":          symbol,
                "side":            decision.side,
                "timeframe":       "15m",
                "confidence":      adjusted_confidence,
                "risk_level":      decision.risk_level,
                "strategy":        "MTF_SMC_STRICT",
                "reasons":         " | ".join(
                    r for r in [*decision.reasons[:6], oi_diag, funding_diag] if r
                ),
                "entry_low":       levels.entry_low,
                "entry_high":      levels.entry_high,
                "tp1":             levels.tp1,
                "tp2":             levels.tp2,
                "tp3":             levels.tp3,
                "stop_loss":       levels.stop_loss,
                "risk_reward":     levels.risk_reward,
                "rr_method":       levels.rr_method,
                "status":          "OPEN",
                "trend_score":     decision.trend_score,
                "structure_score": decision.structure_score,
                "setup_score":     decision.setup_score,
                "entry_score":     decision.entry_score_pts,
                "diagnostics":     diagnostics,
                # Sprint 19A — stored on the Signal row
                "market_regime":   regime.market_regime if regime else None,
                "regime_score":    regime.regime_score if regime else None,
                "_snapshot":           primary_snap,
                "_contributing_tfs":   decision.contributing_tfs,
                "_detected_at":        utcnow(),
                "_tier":               decision.tier,
                "_funding_rate":       funding_data.funding_rate if funding_data else None,
                "_funding_class":      funding_class,
                "_funding_score":      funding_score,
                "_funding_reason":     funding_fs.reason if funding_fs else "",
            }

    async def scan_once(self) -> int:
        symbols = list(universe.symbols)
        if not symbols:
            logger.warning("symbol universe empty — refresh first")
            return 0

        # Sprint 19A: warm market regime cache before scanning symbols
        try:
            await ensure_regime_fresh()
        except Exception as exc:
            logger.warning(f"market regime calculation failed: {exc}")

        tasks = [self._analyze_symbol(s) for s in symbols]
        emitted = 0

        counters: Dict[str, int] = {
            "analyzed":        len(symbols),
            "no_data":         0,
            "trend":           0,
            "structure":       0,
            "setup":           0,
            "entry":           0,
            "confidence":      0,
            "rr":              0,
            "duplicate_active":0,
            "post_close_cooldown": 0,
            "cooldown":        0,
            "rate_limit":      0,
            "short_protection": 0,
            "error":           0,
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

        dup_skip = c["duplicate_active"] + c["post_close_cooldown"]
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
            f"  Short rejected:  {c['short_protection']}\n"
            f"  Dup skipped:     {dup_skip}\n"
            f"  Emitted:         {emitted}\n"
            f"{'─'*52}"
        )

        # ── Sprint 18: Adaptive threshold cycle ──────────────────────────
        if settings.adaptive_thresholds:
            try:
                from app.adaptive.engine import run_adaptive_cycle
                await run_adaptive_cycle()
            except Exception as exc:
                logger.debug(f"adaptive cycle failed: {exc}")

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
