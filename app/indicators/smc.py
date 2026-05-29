"""
Smart Money Concepts (SMC) primitives.

We implement light, deterministic versions that work well on liquid futures
pairs:
- Break of Structure (BOS) — price closes beyond the last swing high/low in
  the direction of the trend.
- Market Structure Shift (MSS) — price closes through the last swing high/low
  AGAINST the prior trend.
- Liquidity sweep — wick pierces a recent swing high/low but body closes back
  inside (classic stop-hunt pattern).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class StructureSignal:
    bos_bull: bool = False
    bos_bear: bool = False
    mss_bull: bool = False
    mss_bear: bool = False
    sweep_bull: bool = False   # bullish liquidity sweep (low wick swept then reclaim)
    sweep_bear: bool = False   # bearish liquidity sweep (high wick swept then rejection)


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
    """
    df must contain open/high/low/close, sorted ascending in time.
    """
    sig = StructureSignal()
    if len(df) < max(lookback + 5, trend_ema_len + 5):
        return sig

    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    opens = df["open"]

    last_high, last_low = _last_swing(highs, lows, lookback)
    if pd.isna(last_high) or pd.isna(last_low):
        return sig

    last_close = float(closes.iloc[-1])
    last_open = float(opens.iloc[-1])
    last_high_bar = float(highs.iloc[-1])
    last_low_bar = float(lows.iloc[-1])

    # trend reference
    ema_trend = closes.ewm(span=trend_ema_len, adjust=False).mean()
    trend_up = float(ema_trend.iloc[-1]) > float(ema_trend.iloc[-5])

    # BOS / MSS
    if last_close > last_high:
        if trend_up:
            sig.bos_bull = True
        else:
            sig.mss_bull = True
    if last_close < last_low:
        if not trend_up:
            sig.bos_bear = True
        else:
            sig.mss_bear = True

    # liquidity sweep: wick beyond swing but close back inside
    body_range = abs(last_close - last_open)
    full_range = max(last_high_bar - last_low_bar, 1e-12)
    body_ratio = body_range / full_range

    if (
        last_low_bar < last_low
        and last_close > last_low
        and last_close > last_open
        and body_ratio < 0.6
    ):
        sig.sweep_bull = True

    if (
        last_high_bar > last_high
        and last_close < last_high
        and last_close < last_open
        and body_ratio < 0.6
    ):
        sig.sweep_bear = True

    return sig
