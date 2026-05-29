"""
Compute entry zone, take profits and stop loss from features.

Logic:
- Entry zone is a small band around current price (~0.15 * ATR each side).
- Stop loss uses ATR + the nearest swing low/high (whichever is further).
- TPs are ATR multiples and RR-based.
- We reject the setup if RR < min_rr.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

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
    risk_reward: float   # to TP2


def build_levels(snap: FeatureSnapshot, side: str) -> Optional[TradeLevels]:
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
        tp1 = price + 1.2 * risk
        tp2 = price + 2.2 * risk
        tp3 = price + 3.5 * risk
        rr = (tp2 - price) / risk
    else:  # SHORT
        swing_sl = snap.recent_high + 0.2 * atr
        atr_sl = price + 1.8 * atr
        sl = max(swing_sl, atr_sl)
        risk = sl - price
        if risk <= 0:
            return None
        tp1 = price - 1.2 * risk
        tp2 = price - 2.2 * risk
        tp3 = price - 3.5 * risk
        rr = (price - tp2) / risk

    if rr < settings.min_rr:
        return None

    return TradeLevels(
        entry_low=min(entry_low, entry_high),
        entry_high=max(entry_low, entry_high),
        stop_loss=sl,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        risk_reward=round(rr, 2),
    )
