"""
Stop-Loss Engine V3 — mode resolution + Balanced ATR/structure stop.

Three stop engines are supported, selected by ``STOPLOSS_ENGINE_MODE``:

  LEGACY_ATR      — the original 15m swing/ATR stop (pre-V2 behaviour). Computed
                    inline in ``app.risk.levels.build_levels``.
  PREV_1D_SUPPORT — Stop-Loss Engine V2: the previous completed 1D candle
                    support/resistance (``compute_prev_1d_stop`` in ``levels``).
  BALANCED        — Stop-Loss Engine V3 (this module): a 15m ATR stop combined
                    with the recent 15m swing, clamped to a sane min/max band.

Why BALANCED exists
-------------------
PREV_1D_SUPPORT places the stop below the previous daily low (LONG) / above the
previous daily high (SHORT). From a 15m entry that is frequently several percent
away, so the resulting SL distance is large, the risk leg is large, and the RR
filter rejects almost every setup → signal starvation. BALANCED scales the stop
to 15m volatility/structure instead, keeping distance inside
``BALANCED_STOP_*_DISTANCE_PERCENT``.

``compute_balanced_stop`` is pure: it never raises and never forces a valid
stop. Inputs that cannot yield a sane stop produce ``sl_valid=False`` with a
``sl_reject_reason`` — the caller then drops the setup. No emission is forced.
"""

from __future__ import annotations

import math
from typing import Optional

from app.config import settings

LONG = "LONG"
SHORT = "SHORT"

# Active-engine mode values.
MODE_LEGACY_ATR = "LEGACY_ATR"
MODE_PREV_1D_SUPPORT = "PREV_1D_SUPPORT"
MODE_BALANCED = "BALANCED"
_VALID_MODES = {MODE_LEGACY_ATR, MODE_PREV_1D_SUPPORT, MODE_BALANCED}


def resolve_stoploss_mode() -> str:
    """
    Resolve the active stop-loss engine mode.

    ``STOPLOSS_ENGINE_MODE`` takes priority when it holds a recognised value.
    When it is empty/unrecognised we fall back to the legacy V2 flags so older
    deployments keep their behaviour:
      * V2 enabled + method PREV_1D_SUPPORT → PREV_1D_SUPPORT
      * otherwise                           → LEGACY_ATR
    """
    mode = (getattr(settings, "stoploss_engine_mode", "") or "").strip().upper()
    if mode in _VALID_MODES:
        return mode

    if settings.stoploss_engine_v2_enabled and settings.stoploss_method == "PREV_1D_SUPPORT":
        return MODE_PREV_1D_SUPPORT
    return MODE_LEGACY_ATR


def balanced_max_distance_percent(
    market_regime: Optional[str],
    *,
    regime_gate_enabled: Optional[bool] = None,
) -> float:
    """
    Effective max SL distance (%) for BALANCED mode.

    Returns ``BALANCED_STOP_MAX_DISTANCE_PERCENT`` unless the Regime Adaptive
    Gate is enabled, in which case the per-regime balanced override applies:
    LOW_VOLATILITY widens, HIGH_VOLATILITY / SIDEWAYS tighten. Other regimes use
    the base value. The min RR / confidence clamps remain owned by the gate.
    """
    base = float(settings.balanced_stop_max_distance_percent)
    enabled = (
        settings.regime_adaptive_gate_enabled
        if regime_gate_enabled is None
        else regime_gate_enabled
    )
    if not enabled:
        return base

    r = (market_regime or "").upper()
    if r == "LOW_VOLATILITY":
        return float(settings.low_vol_balanced_max_distance_percent)
    if r == "HIGH_VOLATILITY":
        return float(settings.high_vol_balanced_max_distance_percent)
    if r == "SIDEWAYS":
        return float(settings.sideways_balanced_max_distance_percent)
    return base


def compute_balanced_stop(
    side: str,
    entry_price: float,
    atr_15m: float,
    recent_swing_low: float,
    recent_swing_high: float,
    *,
    atr_mult: float,
    structure_buffer_atr_mult: float,
    min_pct: float,
    max_pct: float,
) -> dict:
    """
    Balanced ATR + structure stop (Stop-Loss Engine V3). Pure; never raises.

    Two candidates are formed from 15m volatility/structure:

      LONG : atr_stop       = entry - ATR(15m) * atr_mult
             structure_stop = recent_swing_low - ATR(15m) * structure_buffer_mult
      SHORT: atr_stop       = entry + ATR(15m) * atr_mult
             structure_stop = recent_swing_high + ATR(15m) * structure_buffer_mult

    Selection (the "safer but not excessively far" stop):
      1. Pick the more conservative of the two (farther from entry) — the lower
         stop for LONG, the higher stop for SHORT — provided it stays on the
         correct side of entry.
      2. If its distance exceeds ``max_pct`` → fall back to the ATR stop.
      3. If the ATR stop is still beyond ``max_pct`` → reject (``sl_too_far``).
      4. If the chosen stop is closer than ``min_pct`` → widen to the floor.

    The previous-1D support/resistance is never consulted here; it is only
    available when ``BALANCED_STOP_ALLOW_1D_FALLBACK`` is set, handled by the
    caller. Returns a diagnostics dict (Phase 4 schema).
    """
    diag: dict = {
        "stoploss_engine_mode": MODE_BALANCED,
        "stoploss_method": "ATR_STRUCTURE_BALANCED",
        "balanced_atr_stop": None,
        "balanced_structure_stop": None,
        "selected_stop_source": None,
        "stop_loss": None,
        "sl_distance_percent": None,
        "sl_min_distance_percent": round(float(min_pct), 4),
        "sl_max_distance_percent": round(float(max_pct), 4),
        "sl_widened": False,
        "sl_valid": False,
        "sl_reject_reason": None,
    }

    # Reject non-finite inputs before any arithmetic (NaN comparisons are all
    # False and would slip a NaN stop through the guards marked valid).
    if not all(
        math.isfinite(x) for x in (entry_price, atr_15m, recent_swing_low, recent_swing_high)
    ):
        diag["sl_reject_reason"] = "non_finite_input"
        return diag
    if entry_price <= 0:
        diag["sl_reject_reason"] = "invalid_entry_price"
        return diag
    if atr_15m <= 0:
        diag["sl_reject_reason"] = "invalid_atr"
        return diag

    atr = float(atr_15m)
    buffer = float(structure_buffer_atr_mult) * atr

    def _dist(sl: float) -> float:
        if side == LONG:
            return (entry_price - sl) / entry_price * 100.0
        return (sl - entry_price) / entry_price * 100.0

    if side == LONG:
        atr_stop = entry_price - float(atr_mult) * atr
        structure_stop = float(recent_swing_low) - buffer
        diag["balanced_atr_stop"] = round(atr_stop, 8)
        diag["balanced_structure_stop"] = round(structure_stop, 8)

        # Candidates must sit below entry to be valid for a LONG.
        structure_ok = structure_stop < entry_price
        atr_ok = atr_stop < entry_price
        if not atr_ok and not structure_ok:
            diag["sl_reject_reason"] = "no_candidate_below_entry"
            return diag

        # More conservative = the lower stop (farther below entry).
        if structure_ok and (not atr_ok or structure_stop <= atr_stop):
            sl, source = structure_stop, "STRUCTURE"
        else:
            sl, source = atr_stop, "ATR"
    elif side == SHORT:
        atr_stop = entry_price + float(atr_mult) * atr
        structure_stop = float(recent_swing_high) + buffer
        diag["balanced_atr_stop"] = round(atr_stop, 8)
        diag["balanced_structure_stop"] = round(structure_stop, 8)

        structure_ok = structure_stop > entry_price
        atr_ok = atr_stop > entry_price
        if not atr_ok and not structure_ok:
            diag["sl_reject_reason"] = "no_candidate_above_entry"
            return diag

        # More conservative = the higher stop (farther above entry).
        if structure_ok and (not atr_ok or structure_stop >= atr_stop):
            sl, source = structure_stop, "STRUCTURE"
        else:
            sl, source = atr_stop, "ATR"
    else:
        diag["sl_reject_reason"] = "invalid_side"
        return diag

    dist = _dist(sl)

    # Too far → fall back to the ATR stop (rule).
    if dist > max_pct:
        if atr_ok:
            sl, source = atr_stop, "ATR"
            dist = _dist(sl)
        # Still too far after fallback → reject.
        if dist > max_pct:
            diag["selected_stop_source"] = source
            diag["stop_loss"] = round(sl, 8)
            diag["sl_distance_percent"] = round(dist, 4)
            diag["sl_reject_reason"] = "sl_too_far"
            return diag

    # Too close → widen to the minimum-distance floor.
    if dist < min_pct:
        if side == LONG:
            sl = entry_price * (1.0 - min_pct / 100.0)
        else:
            sl = entry_price * (1.0 + min_pct / 100.0)
        dist = min_pct
        diag["sl_widened"] = True

    diag["selected_stop_source"] = source
    diag["stop_loss"] = round(sl, 8)
    diag["sl_distance_percent"] = round(dist, 4)
    diag["sl_valid"] = True
    return diag
