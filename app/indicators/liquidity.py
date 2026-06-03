"""
Sprint 17 — Liquidity Sweep Engine.

Detects stop hunts and fake breakouts using equal highs/lows and wick analysis.
Enable via ENABLE_LIQUIDITY_ENGINE=true.

Patterns detected:
  - Equal Highs / Equal Lows       (clustered liquidity pools)
  - Liquidity Sweep Up / Down      (wick pierces pool, body closes inside)
  - Fake Breakout Up / Down        (close beyond pool then reverses next bar)
  - Swing Failure Pattern Bull/Bear (SFP — new high/low that fails to hold)
  - Stop Hunt Bull / Bear          (aggressive wick into stop-cluster zone)

liquidity_score: 0-20 total.
liquidity_score_for_side: 0-20 directional (only counts patterns favoring side).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

_TOLERANCE_PCT = 0.10  # 0.1% — highs/lows within this band are "equal"


@dataclass
class LiquiditySignal:
    # Pool detection
    equal_highs: bool = False
    equal_lows: bool = False
    eq_high_level: float = 0.0  # price of the equal-highs cluster
    eq_low_level: float = 0.0  # price of the equal-lows cluster

    # Directional patterns
    sweep_up: bool = False  # wick > eq_highs, body closed below
    sweep_down: bool = False  # wick < eq_lows, body closed above
    fake_breakout_up: bool = False  # prior bar closed above eq_highs, now reversed
    fake_breakout_down: bool = False  # prior bar closed below eq_lows, now reversed
    stop_hunt_bull: bool = False  # aggressive down-wick below swing low (bear trap)
    stop_hunt_bear: bool = False  # aggressive up-wick above swing high (bull trap)
    swing_failure_bull: bool = False  # SFP: new high on wick, bearish close (bull trap)
    swing_failure_bear: bool = False  # SFP: new low on wick, bullish close (bear trap)

    score: int = 0  # sum of all patterns (non-directional), 0-20


def analyze_liquidity(df: pd.DataFrame, lookback: int = 30) -> LiquiditySignal:
    """
    Analyze df (open/high/low/close, ascending) for liquidity patterns.
    Requires at least lookback + 5 rows.
    """
    sig = LiquiditySignal()
    if len(df) < lookback + 5:
        return sig

    highs = df["high"].values
    lows = df["low"].values
    closes = df["close"].values
    opens = df["open"].values

    last_close = float(closes[-1])
    last_high = float(highs[-1])
    last_low = float(lows[-1])
    last_open = float(opens[-1])

    window_h = highs[-lookback - 1 : -1]
    window_l = lows[-lookback - 1 : -1]
    tol = last_close * _TOLERANCE_PCT / 100.0

    # ── Equal Highs ───────────────────────────────────────────────────────
    eq_high_level = 0.0
    for i in range(len(window_h)):
        for j in range(i + 1, len(window_h)):
            if abs(window_h[i] - window_h[j]) <= tol:
                sig.equal_highs = True
                eq_high_level = max(eq_high_level, window_h[i], window_h[j])
    sig.eq_high_level = eq_high_level

    # ── Equal Lows ────────────────────────────────────────────────────────
    eq_low_level = float("inf")
    found_low = False
    for i in range(len(window_l)):
        for j in range(i + 1, len(window_l)):
            if abs(window_l[i] - window_l[j]) <= tol:
                sig.equal_lows = True
                found_low = True
                eq_low_level = min(eq_low_level, window_l[i], window_l[j])
    sig.eq_low_level = eq_low_level if found_low else 0.0
    if not found_low:
        eq_low_level = 0.0

    # ── Liquidity Sweep Up ────────────────────────────────────────────────
    if sig.equal_highs and eq_high_level > 0:
        if last_high > eq_high_level and last_close < eq_high_level:
            sig.sweep_up = True

    # ── Liquidity Sweep Down ──────────────────────────────────────────────
    if sig.equal_lows and eq_low_level > 0:
        if last_low < eq_low_level and last_close > eq_low_level:
            sig.sweep_down = True

    # ── Fake Breakout Up ─────────────────────────────────────────────────
    if len(closes) >= 2 and sig.equal_highs and eq_high_level > 0:
        if closes[-2] > eq_high_level and last_close < eq_high_level:
            sig.fake_breakout_up = True

    # ── Fake Breakout Down ────────────────────────────────────────────────
    if len(closes) >= 2 and sig.equal_lows and eq_low_level > 0:
        if closes[-2] < eq_low_level and last_close > eq_low_level:
            sig.fake_breakout_down = True

    # ── Swing Failure Pattern ─────────────────────────────────────────────
    n_swing_high = float(np.max(window_h)) if len(window_h) else last_high
    n_swing_low = float(np.min(window_l)) if len(window_l) else last_low

    # SFP Bull: new high on wick, bar closes bearish — bull trap
    if last_high > n_swing_high and last_close < last_open:
        sig.swing_failure_bull = True

    # SFP Bear: new low on wick, bar closes bullish — bear trap
    if last_low < n_swing_low and last_close > last_open:
        sig.swing_failure_bear = True

    # ── Stop Hunts ────────────────────────────────────────────────────────
    body = abs(last_close - last_open)
    wick_down = min(last_close, last_open) - last_low
    wick_up = last_high - max(last_close, last_open)

    if body > 0:
        if wick_down > 2.0 * body and last_low < n_swing_low:
            sig.stop_hunt_bull = True
        if wick_up > 2.0 * body and last_high > n_swing_high:
            sig.stop_hunt_bear = True

    # ── Total score (all patterns, non-directional) ───────────────────────
    all_patterns = [
        sig.equal_highs,
        sig.equal_lows,
        sig.sweep_up,
        sig.sweep_down,
        sig.fake_breakout_up,
        sig.fake_breakout_down,
        sig.stop_hunt_bull,
        sig.stop_hunt_bear,
        sig.swing_failure_bull,
        sig.swing_failure_bear,
    ]
    sig.score = min(20, sum(2 for p in all_patterns if p))

    return sig


def liquidity_score_for_side(sig: LiquiditySignal, side: str) -> int:
    """
    Return a directional score (0-20) counting only patterns that favour `side`.

    LONG  favours: sweep_down, fake_breakout_down, stop_hunt_bull, swing_failure_bear, equal_lows
    SHORT favours: sweep_up, fake_breakout_up, stop_hunt_bear, swing_failure_bull, equal_highs
    """
    if side == "LONG":
        patterns = [
            sig.sweep_down,  # bears got their liquidity, reversal likely
            sig.fake_breakout_down,  # failed breakdown = bullish
            sig.stop_hunt_bull,  # bear stops flushed, bulls now free
            sig.swing_failure_bear,  # new low rejected, bulls take over
            sig.equal_lows,  # demand zone (stops clustered below)
        ]
    else:
        patterns = [
            sig.sweep_up,  # bulls got their liquidity, reversal likely
            sig.fake_breakout_up,  # failed breakout = bearish
            sig.stop_hunt_bear,  # bull stops flushed, bears now free
            sig.swing_failure_bull,  # new high rejected, bears take over
            sig.equal_highs,  # supply zone (stops clustered above)
        ]
    return min(20, sum(2 for p in patterns if p))
