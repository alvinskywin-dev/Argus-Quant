"""
Sanity tests for AI scoring + feature snapshot.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.ai_scoring import aggregate, score_side
from app.strategies import build_snapshot


def _trending_df(n: int = 250, up: bool = True) -> pd.DataFrame:
    np.random.seed(11 if up else 13)
    drift = 0.15 if up else -0.15
    noise = np.random.randn(n) * 0.3
    price = 100 + np.cumsum(np.full(n, drift) + noise)
    high = price + np.abs(np.random.randn(n) * 0.2) + 0.1
    low = price - np.abs(np.random.randn(n) * 0.2) - 0.1
    open_ = price + np.random.randn(n) * 0.1
    vol = np.abs(np.random.randn(n) * 1000 + 5000)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": price, "volume": vol})


def test_snapshot_builds():
    df = _trending_df(up=True)
    snap = build_snapshot("FAKEUSDT", "15m", df)
    assert snap is not None
    assert snap.atr_value > 0
    assert 0 <= snap.bb_pos <= 1


def test_long_outscores_short_in_uptrend():
    df = _trending_df(up=True)
    snap = build_snapshot("FAKEUSDT", "15m", df)
    long_s = score_side(snap, "LONG")
    short_s = score_side(snap, "SHORT")
    assert long_s.confidence >= short_s.confidence


def test_short_outscores_long_in_downtrend():
    df = _trending_df(up=False)
    snap = build_snapshot("FAKEUSDT", "15m", df)
    long_s = score_side(snap, "LONG")
    short_s = score_side(snap, "SHORT")
    assert short_s.confidence >= long_s.confidence


def test_mtf_aggregator_returns_decision():
    df15 = _trending_df(up=True)
    df1h = _trending_df(up=True)
    s15 = score_side(build_snapshot("X", "15m", df15), "LONG")
    s1h = score_side(build_snapshot("X", "1h", df1h), "LONG")
    decision = aggregate({"15m": s15, "1h": s1h}, primary_tf="15m")
    assert decision is not None
    assert decision.side == "LONG"
    assert 0 <= decision.confidence <= 100
