"""
Smart Money Concepts (SMC) primitives.

Implements deterministic detectors for liquid futures pairs:
- BOS  — Break of Structure
- CHoCH/MSS — Change of Character / Market Structure Shift
- Liquidity sweep — wick beyond swing, body closes back inside
- FVG  — Fair Value Gap (price imbalance between candle[i-2] and candle[i])
- OB   — Order Block (last counter-trend candle before an impulse)
- Pullback — recent counter-trend retracement inside a larger trend
- Retest — price returning to test a freshly broken level
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class StructureSignal:
    bos_bull: bool = False
    bos_bear: bool = False
    mss_bull: bool = False  # Change of Character bullish
    mss_bear: bool = False  # Change of Character bearish
    sweep_bull: bool = False
    sweep_bear: bool = False
    fvg_bull: bool = False
    fvg_bear: bool = False
    ob_bull: bool = False
    ob_bear: bool = False
    pullback_bull: bool = False
    pullback_bear: bool = False
    retest_bull: bool = False
    retest_bear: bool = False


def _last_swing(highs: pd.Series, lows: pd.Series, lookback: int) -> tuple[float, float]:
    seg_h = highs.iloc[-lookback - 2 : -2]
    seg_l = lows.iloc[-lookback - 2 : -2]
    return (
        float(seg_h.max()) if not seg_h.empty else float("nan"),
        float(seg_l.min()) if not seg_l.empty else float("nan"),
    )


def analyze_structure(
    df: pd.DataFrame,
    lookback: int = 20,
    trend_ema_len: int = 50,
) -> StructureSignal:
    """df must contain open/high/low/close sorted ascending in time."""
    sig = StructureSignal()
    if len(df) < max(lookback + 5, trend_ema_len + 5):
        return sig

    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    opens = df["open"]

    last_swing_high, last_swing_low = _last_swing(highs, lows, lookback)
    if pd.isna(last_swing_high) or pd.isna(last_swing_low):
        return sig

    last_close = float(closes.iloc[-1])
    last_open = float(opens.iloc[-1])
    last_high_bar = float(highs.iloc[-1])
    last_low_bar = float(lows.iloc[-1])

    ema_trend = closes.ewm(span=trend_ema_len, adjust=False).mean()
    trend_up = float(ema_trend.iloc[-1]) > float(ema_trend.iloc[-5])

    # ── BOS / CHoCH ──────────────────────────────────────────────────────
    if last_close > last_swing_high:
        if trend_up:
            sig.bos_bull = True
        else:
            sig.mss_bull = True
    if last_close < last_swing_low:
        if not trend_up:
            sig.bos_bear = True
        else:
            sig.mss_bear = True

    # ── Liquidity sweep ──────────────────────────────────────────────────
    body_range = abs(last_close - last_open)
    full_range = max(last_high_bar - last_low_bar, 1e-12)
    body_ratio = body_range / full_range

    if (
        last_low_bar < last_swing_low
        and last_close > last_swing_low
        and last_close > last_open
        and body_ratio < 0.6
    ):
        sig.sweep_bull = True

    if (
        last_high_bar > last_swing_high
        and last_close < last_swing_high
        and last_close < last_open
        and body_ratio < 0.6
    ):
        sig.sweep_bear = True

    # ── FVG (Fair Value Gap) ──────────────────────────────────────────────
    scan_end = len(df) - 1
    scan_start = max(2, scan_end - 12)
    for i in range(scan_start, scan_end):
        c1_high = float(highs.iloc[i - 2])
        c1_low = float(lows.iloc[i - 2])
        c3_high = float(highs.iloc[i])
        c3_low = float(lows.iloc[i])
        if c3_low > c1_high:
            sig.fvg_bull = True
        if c3_high < c1_low:
            sig.fvg_bear = True

    # ── Order Block ───────────────────────────────────────────────────────
    ob_lookback = min(15, len(df) - 4)
    c_arr = closes.values
    o_arr = opens.values
    h_arr = highs.values
    l_arr = lows.values

    for i in range(len(df) - ob_lookback, len(df) - 3):
        if i < 1:
            continue
        is_bearish = c_arr[i] < o_arr[i]
        is_bullish = c_arr[i] > o_arr[i]

        next3_high = float(np.max(h_arr[i + 1 : i + 4]))
        next3_low = float(np.min(l_arr[i + 1 : i + 4]))

        if is_bearish and next3_high > h_arr[i]:
            # Bearish OB followed by bullish impulse — bull OB in play
            ob_high = float(h_arr[i])
            ob_low = float(l_arr[i])
            if ob_low <= last_close <= ob_high * 1.005:
                sig.ob_bull = True

        if is_bullish and next3_low < l_arr[i]:
            # Bullish OB followed by bearish impulse — bear OB in play
            ob_high = float(h_arr[i])
            ob_low = float(l_arr[i])
            if ob_low * 0.995 <= last_close <= ob_high:
                sig.ob_bear = True

    # ── Pullback ─────────────────────────────────────────────────────────
    # Compares recent 5-bar micro-trend against the larger EMA trend direction.
    if len(closes) >= 6:
        recent_start = float(closes.iloc[-5])
        recent_end = float(closes.iloc[-1])
        recent_up = recent_end > recent_start
        sig.pullback_bull = trend_up and not recent_up
        sig.pullback_bear = not trend_up and recent_up

    # ── Retest ────────────────────────────────────────────────────────────
    # Price has broken a level and is now retesting it from the other side.
    if last_swing_high > 0:
        near_high = abs(last_close - last_swing_high) / last_swing_high < 0.004
        sig.retest_bull = sig.bos_bull and near_high and last_close >= last_swing_high * 0.997

    if last_swing_low > 0:
        near_low = abs(last_close - last_swing_low) / last_swing_low < 0.004
        sig.retest_bear = sig.bos_bear and near_low and last_close <= last_swing_low * 1.003

    return sig
