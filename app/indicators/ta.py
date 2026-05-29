"""
Technical indicators implemented in vectorized NumPy/Pandas.
All functions take a DataFrame with columns: open, high, low, close, volume.
Return a Series (or dict of Series) aligned with the input index.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


# ---------- Moving averages ----------
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


# ---------- Oscillators ----------
def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def stoch_rsi(close: pd.Series, rsi_len: int = 14, stoch_len: int = 14,
              k_smooth: int = 3, d_smooth: int = 3) -> Dict[str, pd.Series]:
    r = rsi(close, rsi_len)
    min_r = r.rolling(stoch_len).min()
    max_r = r.rolling(stoch_len).max()
    stoch = 100 * (r - min_r) / (max_r - min_r).replace(0, np.nan)
    k = stoch.rolling(k_smooth).mean()
    d = k.rolling(d_smooth).mean()
    return {"k": k, "d": d}


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, pd.Series]:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    hist = line - sig
    return {"macd": line, "signal": sig, "hist": hist}


# ---------- Bands ----------
def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0) -> Dict[str, pd.Series]:
    mid = sma(close, length)
    std = close.rolling(length).std()
    upper = mid + mult * std
    lower = mid - mult * std
    width = (upper - lower) / mid
    return {"upper": upper, "mid": mid, "lower": lower, "width": width}


# ---------- Volatility ----------
def true_range(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / length, adjust=False).mean()


# ---------- VWAP (intraday rolling) ----------
def vwap(df: pd.DataFrame, length: int = 20) -> pd.Series:
    pv = ((df["high"] + df["low"] + df["close"]) / 3) * df["volume"]
    return pv.rolling(length).sum() / df["volume"].rolling(length).sum().replace(0, np.nan)


# ---------- Supertrend ----------
def supertrend(df: pd.DataFrame, length: int = 10, mult: float = 3.0) -> Dict[str, pd.Series]:
    hl2 = (df["high"] + df["low"]) / 2
    _atr = atr(df, length)
    upper_basic = hl2 + mult * _atr
    lower_basic = hl2 - mult * _atr

    upper = upper_basic.copy()
    lower = lower_basic.copy()
    direction = pd.Series(index=df.index, dtype=float)
    st = pd.Series(index=df.index, dtype=float)

    close = df["close"]
    for i in range(1, len(df)):
        if close.iloc[i - 1] <= upper.iloc[i - 1]:
            upper.iloc[i] = min(upper_basic.iloc[i], upper.iloc[i - 1])
        if close.iloc[i - 1] >= lower.iloc[i - 1]:
            lower.iloc[i] = max(lower_basic.iloc[i], lower.iloc[i - 1])

        if pd.isna(direction.iloc[i - 1]):
            direction.iloc[i] = 1.0
        else:
            direction.iloc[i] = direction.iloc[i - 1]

        if direction.iloc[i] == 1.0 and close.iloc[i] < lower.iloc[i]:
            direction.iloc[i] = -1.0
        elif direction.iloc[i] == -1.0 and close.iloc[i] > upper.iloc[i]:
            direction.iloc[i] = 1.0

        st.iloc[i] = lower.iloc[i] if direction.iloc[i] == 1.0 else upper.iloc[i]

    return {"supertrend": st, "direction": direction}


# ---------- ADX ----------
def adx(df: pd.DataFrame, length: int = 14) -> Dict[str, pd.Series]:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = true_range(df)
    atr_ = tr.ewm(alpha=1 / length, adjust=False).mean()

    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / length, adjust=False).mean() / atr_

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = dx.ewm(alpha=1 / length, adjust=False).mean()
    return {"adx": adx_, "plus_di": plus_di, "minus_di": minus_di}


# ---------- Volume utilities ----------
def volume_zscore(volume: pd.Series, length: int = 20) -> pd.Series:
    mean = volume.rolling(length).mean()
    std = volume.rolling(length).std()
    return (volume - mean) / std.replace(0, np.nan)


def volume_spike_pct(volume: pd.Series, length: int = 20) -> pd.Series:
    mean = volume.rolling(length).mean()
    return (volume / mean.replace(0, np.nan) - 1) * 100


# ---------- Support / resistance (swing pivots) ----------
def swing_pivots(df: pd.DataFrame, left: int = 3, right: int = 3) -> Dict[str, pd.Series]:
    highs = df["high"]
    lows = df["low"]
    pivot_high = highs[(highs.shift(left).rolling(left).max() < highs) &
                       (highs.shift(-right).rolling(right).max() < highs)]
    pivot_low = lows[(lows.shift(left).rolling(left).min() > lows) &
                     (lows.shift(-right).rolling(right).min() > lows)]
    return {"pivot_high": pivot_high, "pivot_low": pivot_low}
