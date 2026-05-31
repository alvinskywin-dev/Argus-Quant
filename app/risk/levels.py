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

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from app.config import settings
from app.strategies.features import FeatureSnapshot


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
    liq_signal,          # Optional[LiquiditySignal] — avoid circular import
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


def build_levels(
    snap: FeatureSnapshot,
    side: str,
    liq_signal=None,  # Optional[LiquiditySignal]
) -> Optional[TradeLevels]:
    """
    Compute trade levels with dynamic RR selection.

    Returns None if no candidate RR meets settings.min_rr.
    """
    price = snap.last_close
    atr = max(snap.atr_value, price * 0.001)

    entry_pad = 0.15 * atr
    entry_low = price - entry_pad
    entry_high = price + entry_pad

    if side == "LONG":
        swing_sl = snap.recent_low - 0.2 * atr
        atr_sl = price - 1.8 * atr
        sl = min(swing_sl, atr_sl)
        risk = price - sl
        if risk <= 0:
            return None

        candidates = _candidates_long(price, risk, snap, liq_signal)
        valid = [(m, rr, tp2) for m, rr, tp2 in candidates if rr >= settings.min_rr]
        if not valid:
            return None

        rr_method, rr, tp2 = max(valid, key=lambda x: x[1])
        tp1 = price + 1.2 * risk
        tp3 = price + 3.5 * risk

    else:  # SHORT
        swing_sl = snap.recent_high + 0.2 * atr
        atr_sl = price + 1.8 * atr
        sl = max(swing_sl, atr_sl)
        risk = sl - price
        if risk <= 0:
            return None

        candidates = _candidates_short(price, risk, snap, liq_signal)
        valid = [(m, rr, tp2) for m, rr, tp2 in candidates if rr >= settings.min_rr]
        if not valid:
            return None

        rr_method, rr, tp2 = max(valid, key=lambda x: x[1])
        tp1 = price - 1.2 * risk
        tp3 = price - 3.5 * risk

    return TradeLevels(
        entry_low=min(entry_low, entry_high),
        entry_high=max(entry_low, entry_high),
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        risk_reward=round(rr, 2),
        rr_method=rr_method,
    )
