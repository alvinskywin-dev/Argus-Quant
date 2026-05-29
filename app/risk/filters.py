"""
Smart market filters and rate-limiting safeguards.

These keep the signal stream clean — no spam, no chop, no duplicates.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Tuple

from app.ai_scoring import MTFDecision
from app.config import settings
from app.database import repo
from app.strategies.features import FeatureSnapshot
from app.utils.logger import logger
from app.market_data.ws_engine import latest_prices, market_bias


# ---------- in-memory cooldown / dedup ----------
class CooldownTracker:
    """Per-symbol, per-side cooldown (also fed by DB for restart safety)."""

    def __init__(self) -> None:
        self._last: Dict[Tuple[str, str], datetime] = {}
        self._lock = asyncio.Lock()

    async def can_emit(self, symbol: str, side: str) -> bool:
        async with self._lock:
            now = datetime.now(timezone.utc)
            key = (symbol, side)
            last = self._last.get(key)
            if last and (now - last).total_seconds() < settings.symbol_cooldown_minutes * 60:
                return False

            # Also check DB — survives restarts
            last_db = await repo.last_signal_for(symbol, side)
            if last_db and (now - last_db.created_at).total_seconds() < settings.symbol_cooldown_minutes * 60:
                self._last[key] = last_db.created_at
                return False
            return True

    async def mark_emitted(self, symbol: str, side: str) -> None:
        async with self._lock:
            self._last[(symbol, side)] = datetime.now(timezone.utc)


cooldown = CooldownTracker()


# ---------- per-hour rate cap ----------
class HourlyRateLimiter:
    def __init__(self, max_per_hour: int) -> None:
        self.max = max_per_hour
        self._times: Deque[datetime] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=1)
            while self._times and self._times[0] < cutoff:
                self._times.popleft()
            if len(self._times) >= self.max:
                return False
            self._times.append(now)
            return True

    async def used(self) -> int:
        async with self._lock:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=1)
            while self._times and self._times[0] < cutoff:
                self._times.popleft()
            return len(self._times)


rate_limiter = HourlyRateLimiter(settings.max_signals_per_hour)


# ---------- quality filters ----------
def passes_market_filters(snap: FeatureSnapshot, decision: MTFDecision) -> tuple[bool, str | None]:
    """Returns (ok, reject_reason_or_None)."""
    # low volume
    if snap.vol_spike_pct < -50:
        return False, "low_volume"
    # chop: tight BB + ADX < 18
    if snap.bb_width < 0.02 and snap.trend_strength_adx < 18:
        return False, "chop"
    # overextension
    if decision.side == "LONG" and snap.overextended_long:
        return False, "overextended_long"
    if decision.side == "SHORT" and snap.overextended_short:
        return False, "overextended_short"
    # extreme volatility (likely news spike)
    if snap.atr_pct > 8.0:
        return False, "extreme_volatility"
    # fake breakout probability
    if decision.fake_breakout_prob >= 0.6:
        return False, "fake_breakout_prob"
    # strong trend bonus
    if snap.trend_strength_adx >= 35:
        decision.confidence += 4

    # weak trend penalty
    if snap.trend_strength_adx < 20:
        decision.confidence -= 6

    # momentum bonus
    if abs(getattr(snap, 'price_change_pct_5m', 0) or 0) > 2.5:
        decision.confidence += 3

    # heavy volatility penalty
    if snap.atr_pct > 5:
        decision.confidence -= 4

    # overbought longs penalty
    if decision.side == "LONG" and getattr(snap, 'rsi', getattr(snap, 'rsi_value', 50)) > 72:
        decision.confidence -= 8

    # oversold shorts penalty
    if decision.side == "SHORT" and getattr(snap, 'rsi', getattr(snap, 'rsi_value', 50)) < 28:
        decision.confidence -= 8

    # simple market sentiment guard from live major prices cache
    # If major-price cache is alive, require extra caution on weak contexts.
    if latest_prices:
        bias = market_bias().get("bias")

        if bias == "RISK_OFF" and decision.side == "LONG":
            decision.confidence -= 7

        if bias == "RISK_ON" and decision.side == "SHORT":
            decision.confidence -= 5

        if bias == "NEUTRAL" and snap.trend_strength_adx < 25:
            decision.confidence -= 4

    # confidence floor
    if decision.confidence < settings.min_confidence:
        logger.debug(
            f"CONF_FLOOR {decision.side} final_conf={decision.confidence:.1f} "
            f"threshold={settings.min_confidence} gap={settings.min_confidence - decision.confidence:.1f}"
        )
        return False, "below_confidence_threshold"
    return True, None
