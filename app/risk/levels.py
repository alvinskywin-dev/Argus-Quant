"""
Sprint 16C — Dynamic RR Engine.

Compute entry zone, take-profits and stop-loss from features.

Three RR methods are evaluated and the best (highest valid RR) is selected:

  atr       — TP2 is a volatility-scaled ATR multiple of risk.
               Multiplier: 2.5 (low vol) → 1.8 (high vol), linear in ATR%.
  structure — TP2 is set at the nearest swing high/low (recent_high / recent_low).
               Only used if the structural target gives RR ≥ min_rr.
  liquidity — TP2 is the equal-highs or equal-lows level from the Liquidity Engine.
               Only used when ENABLE_LIQUIDITY_ENGINE=true and a pool is detected.

The selected rr_method and rr_value are stored on the signal for diagnostics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.config import settings
from app.risk.stoploss_modes import (
    MODE_BALANCED,
    MODE_PREV_1D_SUPPORT,
    balanced_max_distance_percent,
    compute_balanced_stop,
    resolve_stoploss_mode,
)
from app.strategies.features import FeatureSnapshot
from app.utils.logger import logger

LONG = "LONG"
SHORT = "SHORT"


@dataclass
class TradeLevels:
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_reward: float  # RR to TP2
    rr_method: str = "atr"  # "atr" | "structure" | "liquidity"
    sl_diag: dict = field(default_factory=dict)  # stop-loss diagnostics (V2)


# ---------- helpers ----------


def _atr_multiplier(atr_pct: float) -> float:
    """
    Scale the TP2 multiplier based on ATR% of price.
    Low volatility → wider targets (2.5×); high volatility → tighter (1.8×).
    Linear interpolation in the 0.5-3.0% band.
    """
    if atr_pct <= 0.5:
        return 2.5
    if atr_pct >= 3.0:
        return 1.8
    t = (atr_pct - 0.5) / (3.0 - 0.5)
    return round(2.5 - t * (2.5 - 1.8), 3)


def _candidates_long(
    price: float,
    risk: float,
    snap: FeatureSnapshot,
    liq_signal,  # Optional[LiquiditySignal] — avoid circular import
) -> List[Tuple[str, float, float]]:
    """Return list of (method, rr, tp2) for LONG side, best to worst."""
    results: List[Tuple[str, float, float]] = []

    # ATR RR
    mult = _atr_multiplier(snap.atr_pct)
    tp2_atr = price + mult * risk
    results.append(("atr", mult, tp2_atr))

    # Structure RR — use 40-bar swing high as TP2
    tp2_struct = snap.recent_high
    if tp2_struct > price + 1.5 * risk:
        struct_rr = (tp2_struct - price) / risk
        results.append(("structure", struct_rr, tp2_struct))

    # Liquidity RR — equal-highs cluster as TP2
    if liq_signal is not None and liq_signal.equal_highs and liq_signal.eq_high_level > 0:
        liq_tp2 = liq_signal.eq_high_level
        if liq_tp2 > price + 1.5 * risk:
            liq_rr = (liq_tp2 - price) / risk
            results.append(("liquidity", liq_rr, liq_tp2))

    return results


def _candidates_short(
    price: float,
    risk: float,
    snap: FeatureSnapshot,
    liq_signal,
) -> List[Tuple[str, float, float]]:
    """Return list of (method, rr, tp2) for SHORT side."""
    results: List[Tuple[str, float, float]] = []

    # ATR RR
    mult = _atr_multiplier(snap.atr_pct)
    tp2_atr = price - mult * risk
    results.append(("atr", mult, tp2_atr))

    # Structure RR — use 40-bar swing low as TP2
    tp2_struct = snap.recent_low
    if tp2_struct < price - 1.5 * risk:
        struct_rr = (price - tp2_struct) / risk
        results.append(("structure", struct_rr, tp2_struct))

    # Liquidity RR — equal-lows cluster as TP2
    if liq_signal is not None and liq_signal.equal_lows and liq_signal.eq_low_level > 0:
        liq_tp2 = liq_signal.eq_low_level
        if liq_tp2 < price - 1.5 * risk:
            liq_rr = (price - liq_tp2) / risk
            results.append(("liquidity", liq_rr, liq_tp2))

    return results


# ---------- Stop-Loss Engine V2 (previous-1D support/resistance) ----------


def compute_prev_1d_stop(
    side: str,
    entry_price: float,
    prev_1d_low: float,
    prev_1d_high: float,
    atr_1d: float,
    *,
    buffer_mult: float,
    min_pct: float,
    max_pct: float,
    too_close_action: str = "widen",
) -> dict:
    """
    Pure SL computation from the previous completed 1D candle.

    LONG : stop_loss = prev_1d_low  - buffer   (must be < entry_price)
    SHORT: stop_loss = prev_1d_high + buffer   (must be > entry_price)
    buffer = buffer_mult * ATR(1D).

    Safety: reject if on the wrong side or farther than max_pct; if closer than
    min_pct either widen to the floor (default) or reject. Returns a diagnostics
    dict — never raises.
    """
    buffer = max(0.0, float(buffer_mult) * max(0.0, float(atr_1d)))
    diag = {
        "stoploss_method": "PREV_1D_SUPPORT",
        "prev_1d_low": round(float(prev_1d_low), 8),
        "prev_1d_high": round(float(prev_1d_high), 8),
        "sl_buffer": round(buffer, 8),
        "base_sl": None,
        "stop_loss": None,
        "sl_distance_percent": None,
        "sl_valid": False,
        "sl_reject_reason": None,
    }

    # Reject non-finite inputs before any arithmetic. A NaN low/high/ATR
    # otherwise slips through every guard below (all NaN comparisons are False)
    # and yields a NaN stop_loss marked sl_valid=True — a silent hazard. Finite
    # inputs are unaffected, so the valid-path SL math is unchanged.
    if not all(math.isfinite(x) for x in (entry_price, prev_1d_low, prev_1d_high, atr_1d)):
        diag["sl_reject_reason"] = "non_finite_input"
        return diag

    if entry_price <= 0:
        diag["sl_reject_reason"] = "invalid_entry_price"
        return diag

    if side == LONG:
        base_sl = float(prev_1d_low)
        sl = base_sl - buffer
        diag["base_sl"] = round(base_sl, 8)
        if sl >= entry_price:
            diag["sl_reject_reason"] = "support_not_below_entry"
            diag["stop_loss"] = round(sl, 8)
            return diag
        dist = (entry_price - sl) / entry_price * 100.0
    else:  # SHORT
        base_sl = float(prev_1d_high)
        sl = base_sl + buffer
        diag["base_sl"] = round(base_sl, 8)
        if sl <= entry_price:
            diag["sl_reject_reason"] = "resistance_not_above_entry"
            diag["stop_loss"] = round(sl, 8)
            return diag
        dist = (sl - entry_price) / entry_price * 100.0

    # Safety rule #2 — too far → reject
    if dist > max_pct:
        diag["stop_loss"] = round(sl, 8)
        diag["sl_distance_percent"] = round(dist, 4)
        diag["sl_reject_reason"] = "sl_too_far"
        return diag

    # Safety rule #3 — too close → widen to floor (preferred) or reject
    if dist < min_pct:
        if too_close_action == "reject":
            diag["stop_loss"] = round(sl, 8)
            diag["sl_distance_percent"] = round(dist, 4)
            diag["sl_reject_reason"] = "sl_too_close"
            return diag
        # widen using the minimum distance floor
        if side == LONG:
            sl = entry_price * (1.0 - min_pct / 100.0)
        else:
            sl = entry_price * (1.0 + min_pct / 100.0)
        dist = min_pct
        diag["sl_widened"] = True

    diag["stop_loss"] = round(sl, 8)
    diag["sl_distance_percent"] = round(dist, 4)
    diag["sl_valid"] = True
    return diag


def _extract_prev_1d(df_1d) -> Optional[dict]:
    """
    Previous completed daily candle OHLC + ATR(1D) from a 1D kline DataFrame.

    Binance returns the in-progress candle as the last row, so the previous
    *completed* candle is iloc[-2]. Returns None if data is insufficient.
    """
    if df_1d is None or len(df_1d) < 16:
        return None
    try:
        from app.indicators import atr as _atr

        prev = df_1d.iloc[-2]
        atr_series = _atr(df_1d, 14).dropna()
        atr_1d = float(atr_series.iloc[-1]) if len(atr_series) else 0.0
        ot = df_1d["open_time"].iloc[-2]
        return {
            "low": float(prev["low"]),
            "high": float(prev["high"]),
            "open": float(prev["open"]),
            "close": float(prev["close"]),
            "time": ot.isoformat() if hasattr(ot, "isoformat") else str(ot),
            "atr_1d": atr_1d,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"prev-1D extract failed: {exc}")
        return None


def build_levels(
    snap: FeatureSnapshot,
    side: str,
    liq_signal=None,  # Optional[LiquiditySignal]
    df_1d=None,  # Optional[pd.DataFrame] — 1D klines for the V2 SL engine
    min_rr: Optional[float] = None,  # override settings.min_rr (Regime Adaptive Gate)
    max_sl_distance_percent: Optional[float] = None,  # override settings.max_sl_distance_percent
    market_regime: Optional[str] = None,  # regime label (BALANCED max-distance adapt)
) -> Optional[TradeLevels]:
    """
    Compute trade levels with dynamic RR selection.

    Returns None if no candidate RR meets the effective min_rr. `min_rr` and
    `max_sl_distance_percent` default to the static settings; the Regime
    Adaptive Gate passes effective values without changing any other logic.
    """
    eff_min_rr = settings.min_rr if min_rr is None else min_rr
    eff_max_sl_pct = (
        settings.max_sl_distance_percent
        if max_sl_distance_percent is None
        else max_sl_distance_percent
    )
    price = snap.last_close
    atr = max(snap.atr_value, price * 0.001)

    entry_pad = 0.15 * atr
    entry_low = price - entry_pad
    entry_high = price + entry_pad

    # ── Stop-Loss Engine: mode dispatch (V3) ──────────────────────────
    # STOPLOSS_ENGINE_MODE selects the active engine (priority over the legacy
    # V2 flag). `mode_sl` is the explicit stop from the chosen engine; when it
    # stays None the side branch falls back to the LEGACY_ATR 15m stop. The
    # engine only sizes the stop — it never forces a signal.
    sl_engine_mode = resolve_stoploss_mode()
    sl_diag: dict = {"stoploss_engine_mode": sl_engine_mode}
    mode_sl: Optional[float] = None

    if sl_engine_mode == MODE_BALANCED:
        bal_max_pct = balanced_max_distance_percent(market_regime)
        res = compute_balanced_stop(
            side,
            price,
            atr,
            snap.recent_low,
            snap.recent_high,
            atr_mult=settings.balanced_stop_atr_mult,
            structure_buffer_atr_mult=settings.balanced_stop_structure_buffer_atr_mult,
            min_pct=settings.balanced_stop_min_distance_percent,
            max_pct=bal_max_pct,
        )
        sl_diag = res
        if res["sl_valid"]:
            mode_sl = float(res["stop_loss"])
        elif settings.balanced_stop_allow_1d_fallback:
            # Last-resort prev-1D candidate, only when explicitly allowed.
            prev = _extract_prev_1d(df_1d)
            if prev is not None:
                fb = compute_prev_1d_stop(
                    side,
                    price,
                    prev["low"],
                    prev["high"],
                    prev["atr_1d"],
                    buffer_mult=settings.stoploss_1d_buffer_atr_mult,
                    min_pct=settings.balanced_stop_min_distance_percent,
                    max_pct=bal_max_pct,
                    too_close_action=settings.stoploss_too_close_action,
                )
                fb["stoploss_engine_mode"] = sl_engine_mode
                fb["selected_stop_source"] = "PREV_1D_FALLBACK"
                fb["sl_min_distance_percent"] = round(
                    float(settings.balanced_stop_min_distance_percent), 4
                )
                fb["sl_max_distance_percent"] = round(float(bal_max_pct), 4)
                sl_diag = fb
                if fb["sl_valid"]:
                    mode_sl = float(fb["stop_loss"])
        if mode_sl is None:
            logger.info(
                f"⛔ {snap.symbol} {side} — SL V3 balanced reject: "
                f"{sl_diag.get('sl_reject_reason')} "
                f"(dist={sl_diag.get('sl_distance_percent')}%)"
            )
            return None

    elif sl_engine_mode == MODE_PREV_1D_SUPPORT:
        prev = _extract_prev_1d(df_1d)
        if prev is not None:
            res = compute_prev_1d_stop(
                side,
                price,
                prev["low"],
                prev["high"],
                prev["atr_1d"],
                buffer_mult=settings.stoploss_1d_buffer_atr_mult,
                min_pct=settings.min_sl_distance_percent,
                max_pct=eff_max_sl_pct,
                too_close_action=settings.stoploss_too_close_action,
            )
            res["prev_1d_time"] = prev["time"]
            res["stoploss_engine_mode"] = sl_engine_mode
            sl_diag = res
            if not res["sl_valid"]:
                logger.info(
                    f"⛔ {snap.symbol} {side} — SL V2 reject: "
                    f"{res['sl_reject_reason']} (dist={res.get('sl_distance_percent')}%)"
                )
                return None
            mode_sl = float(res["stop_loss"])
        else:
            # No usable 1D data → fall back to the legacy stop (do not reject).
            sl_diag = {
                "stoploss_engine_mode": sl_engine_mode,
                "stoploss_method": "PREV_1D_SUPPORT",
                "sl_valid": False,
                "sl_reject_reason": "no_1d_data",
                "fallback": "legacy",
            }

    if side == "LONG":
        if mode_sl is not None:
            sl = mode_sl
        else:
            swing_sl = snap.recent_low - 0.2 * atr
            atr_sl = price - 1.8 * atr
            sl = min(swing_sl, atr_sl)
            sl_diag.setdefault("stoploss_method", "structure" if sl == swing_sl else "atr")
        risk = price - sl
        if risk <= 0:
            return None

        candidates = _candidates_long(price, risk, snap, liq_signal)
        valid = [(m, rr, tp2) for m, rr, tp2 in candidates if rr >= eff_min_rr]
        if not valid:
            return None

        rr_method, rr, tp2 = max(valid, key=lambda x: x[1])
        tp1 = price + 1.2 * risk
        tp3 = price + 3.5 * risk

    else:  # SHORT
        if mode_sl is not None:
            sl = mode_sl
        else:
            swing_sl = snap.recent_high + 0.2 * atr
            atr_sl = price + 1.8 * atr
            sl = max(swing_sl, atr_sl)
            sl_diag.setdefault("stoploss_method", "structure" if sl == swing_sl else "atr")
        risk = sl - price
        if risk <= 0:
            return None

        candidates = _candidates_short(price, risk, snap, liq_signal)
        valid = [(m, rr, tp2) for m, rr, tp2 in candidates if rr >= eff_min_rr]
        if not valid:
            return None

        rr_method, rr, tp2 = max(valid, key=lambda x: x[1])
        tp1 = price - 1.2 * risk
        tp3 = price - 3.5 * risk

    # ── TP ordering guard ────────────────────────────────────────────────
    # tp2 is the dynamic RR target (atr/structure/liquidity); tp3 is a fixed
    # 3.5R. A far structure/liquidity tp2 could otherwise sit beyond tp3 — or a
    # low-RR tp2 below the 1.2R tp1 — so the tracker would tag a "higher" level
    # first and the displayed RR would exceed the real exit. Keep tp2 (the RR
    # anchor) fixed and clamp tp1/tp3 around it to a strict monotonic ladder.
    if side == "LONG":
        tp1 = min(tp1, tp2 - 0.1 * risk)
        tp3 = max(tp3, tp2 + 0.5 * risk)
    else:  # SHORT
        tp1 = max(tp1, tp2 + 0.1 * risk)
        tp3 = min(tp3, tp2 - 0.5 * risk)

    # Finalize SL diagnostics (legacy path or V2 with derived distance).
    sl_diag.setdefault("stop_loss", round(sl, 8))
    if sl_diag.get("sl_distance_percent") is None:
        sl_diag["sl_distance_percent"] = round(abs(price - sl) / price * 100.0, 4)
    sl_diag.setdefault("sl_valid", True)

    return TradeLevels(
        entry_low=min(entry_low, entry_high),
        entry_high=max(entry_low, entry_high),
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        risk_reward=round(rr, 2),
        rr_method=rr_method,
        sl_diag=sl_diag,
    )
