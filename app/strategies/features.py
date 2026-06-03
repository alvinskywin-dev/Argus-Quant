"""
Build a normalized 'feature snapshot' from raw OHLCV for one symbol/timeframe.

The snapshot feeds the AI scoring engine. Everything downstream consumes this
object — keeping it strongly-typed makes the system easier to reason about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from app.indicators import (
    adx,
    atr,
    bollinger,
    ema,
    macd,
    rsi,
    stoch_rsi,
    supertrend,
    volume_spike_pct,
    vwap,
)
from app.indicators.smc import StructureSignal, analyze_structure


@dataclass
class FeatureSnapshot:
    symbol: str
    timeframe: str
    last_close: float
    last_high: float
    last_low: float

    # Trend
    ema_fast: float
    ema_slow: float
    ema_50: float
    ema_200: float
    trend_up: bool
    trend_strength_adx: float
    supertrend_dir: int            # +1 long, -1 short
    macd_hist: float
    macd_cross_up: bool
    macd_cross_down: bool

    # Momentum
    rsi_value: float
    stoch_k: float
    stoch_d: float
    momentum_bull: bool
    momentum_bear: bool

    # Volatility / bands
    atr_value: float
    atr_pct: float                  # ATR as % of price
    bb_width: float
    bb_pos: float                   # where close sits in bb (0 lower, 1 upper)

    # Volume
    vol_spike_pct: float            # last bar volume vs 20-bar avg
    vwap_value: float
    above_vwap: bool

    # Structure (SMC)
    structure: StructureSignal

    # Range / extremes
    range_pct_24bars: float
    overextended_long: bool
    overextended_short: bool

    # Pivot levels used downstream for SL/TP
    recent_high: float
    recent_low: float

    # 1-bar % price change (last close vs previous close)
    price_change_pct: float = field(default=0.0)

    df_len: int = field(default=0)


def build_snapshot(symbol: str, timeframe: str, df: pd.DataFrame) -> Optional[FeatureSnapshot]:
    if df is None or len(df) < 60:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]

    ema_fast = ema(close, 9)
    ema_slow = ema(close, 21)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)

    adx_d = adx(df, 14)
    st = supertrend(df, 10, 3.0)
    macd_d = macd(close)
    bb = bollinger(close, 20, 2.0)
    rsi_v = rsi(close, 14)
    srsi = stoch_rsi(close)
    atr_v = atr(df, 14)
    vwap_v = vwap(df, 20)
    volspk = volume_spike_pct(vol, 20)

    last_close = float(close.iloc[-1])
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])

    ema_fast_v = float(ema_fast.iloc[-1])
    ema_slow_v = float(ema_slow.iloc[-1])
    ema50_v = float(ema50.iloc[-1]) if not np.isnan(ema50.iloc[-1]) else last_close
    ema200_v = float(ema200.iloc[-1]) if not np.isnan(ema200.iloc[-1]) else last_close

    trend_up = ema_fast_v > ema_slow_v and last_close > ema_fast_v
    st_dir = int(st["direction"].iloc[-1]) if not pd.isna(st["direction"].iloc[-1]) else 0
    adx_v = float(adx_d["adx"].iloc[-1])

    macd_hist = float(macd_d["hist"].iloc[-1])
    macd_prev = float(macd_d["hist"].iloc[-2])
    macd_cross_up = macd_prev <= 0 < macd_hist
    macd_cross_down = macd_prev >= 0 > macd_hist

    rsi_val = float(rsi_v.iloc[-1])
    k = float(srsi["k"].iloc[-1])
    d_ = float(srsi["d"].iloc[-1])
    momentum_bull = (rsi_val > 50 and k > d_) or macd_cross_up
    momentum_bear = (rsi_val < 50 and k < d_) or macd_cross_down

    atr_value = float(atr_v.iloc[-1])
    atr_pct = (atr_value / last_close * 100.0) if last_close > 0 else 0.0

    bb_up = float(bb["upper"].iloc[-1])
    bb_lo = float(bb["lower"].iloc[-1])
    bb_w = float(bb["width"].iloc[-1])
    rng = max(bb_up - bb_lo, 1e-12)
    bb_pos = (last_close - bb_lo) / rng
    bb_pos = max(0.0, min(1.0, bb_pos))

    vol_spike = float(volspk.iloc[-1]) if not pd.isna(volspk.iloc[-1]) else 0.0
    vwap_value = float(vwap_v.iloc[-1]) if not pd.isna(vwap_v.iloc[-1]) else last_close
    above_vwap = last_close > vwap_value

    structure = analyze_structure(df, lookback=20, trend_ema_len=50)

    last24 = df.tail(24)
    range_pct = (last24["high"].max() - last24["low"].min()) / max(last24["close"].iloc[0], 1e-12) * 100.0
    overextended_long = bb_pos > 0.97 and rsi_val > 78
    overextended_short = bb_pos < 0.03 and rsi_val < 22

    recent_high = float(df["high"].tail(40).max())
    recent_low = float(df["low"].tail(40).min())

    prev_close = float(close.iloc[-2]) if len(close) >= 2 else last_close
    price_change_pct = (last_close - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0

    return FeatureSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        last_close=last_close,
        last_high=last_high,
        last_low=last_low,
        ema_fast=ema_fast_v,
        ema_slow=ema_slow_v,
        ema_50=ema50_v,
        ema_200=ema200_v,
        trend_up=trend_up,
        trend_strength_adx=adx_v,
        supertrend_dir=st_dir,
        macd_hist=macd_hist,
        macd_cross_up=macd_cross_up,
        macd_cross_down=macd_cross_down,
        rsi_value=rsi_val,
        stoch_k=k,
        stoch_d=d_,
        momentum_bull=momentum_bull,
        momentum_bear=momentum_bear,
        atr_value=atr_value,
        atr_pct=atr_pct,
        bb_width=bb_w,
        bb_pos=bb_pos,
        vol_spike_pct=vol_spike,
        vwap_value=vwap_value,
        above_vwap=above_vwap,
        structure=structure,
        range_pct_24bars=float(range_pct),
        overextended_long=overextended_long,
        overextended_short=overextended_short,
        recent_high=recent_high,
        recent_low=recent_low,
        price_change_pct=round(price_change_pct, 4),
        df_len=len(df),
    )
