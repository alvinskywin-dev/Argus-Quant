"""
Sanity tests for indicators. Run with:
    pytest -q tests
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.indicators import adx, atr, bollinger, ema, macd, rsi, supertrend, vwap


@pytest.fixture
def df() -> pd.DataFrame:
    np.random.seed(7)
    n = 300
    price = 100 + np.cumsum(np.random.randn(n) * 0.5)
    high = price + np.abs(np.random.randn(n) * 0.3)
    low = price - np.abs(np.random.randn(n) * 0.3)
    open_ = price + np.random.randn(n) * 0.1
    vol = np.abs(np.random.randn(n) * 1000 + 5000)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": price, "volume": vol})


def test_ema_length(df):
    out = ema(df["close"], 20)
    assert len(out) == len(df)
    assert not out.iloc[-1] != out.iloc[-1]   # not NaN


def test_rsi_bounds(df):
    out = rsi(df["close"]).dropna()
    assert (out >= 0).all() and (out <= 100).all()


def test_macd_keys(df):
    out = macd(df["close"])
    assert {"macd", "signal", "hist"} <= set(out.keys())


def test_bollinger(df):
    out = bollinger(df["close"])
    assert {"upper", "mid", "lower", "width"} <= set(out.keys())
    last_idx = -1
    assert out["upper"].iloc[last_idx] >= out["mid"].iloc[last_idx] >= out["lower"].iloc[last_idx]


def test_atr_positive(df):
    out = atr(df).dropna()
    assert (out > 0).all()


def test_supertrend_direction(df):
    out = supertrend(df)
    last = out["direction"].dropna().iloc[-1]
    assert last in (1.0, -1.0)


def test_adx_range(df):
    out = adx(df)["adx"].dropna()
    assert (out >= 0).all() and (out <= 100).all()


def test_vwap_positive(df):
    out = vwap(df).dropna()
    assert (out > 0).all()
