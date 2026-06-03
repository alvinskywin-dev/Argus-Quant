"""
Open Interest Engine — Sprint 11A.

Fetches Binance Futures open interest per symbol, computes rolling changes
across 5m / 15m / 1h windows (via Redis ring-buffer), and produces a
directional OI score that feeds the confidence engine.

Score rules (uses 15m OI change as primary):
    LONG  — price_up   + oi_up:   +15   (smart money building longs)
    LONG  — price_up   + oi_down: -10   (longs being closed on the move)
    SHORT — price_down + oi_up:   +15   (smart money building shorts)
    SHORT — price_down + oi_down: -10   (shorts being covered on the move)
    Any other combination:          0
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from app.market_data.binance_client import get_client
from app.market_data.cache import cache_get, cache_set
from app.utils.logger import logger

# Redis TTL per window — 2× the window so the baseline persists across brief gaps.
_TTL: dict[str, int] = {
    "5m": 600,
    "15m": 1800,
    "1h": 7200,
}
_WINDOW_SEC: dict[str, int] = {"5m": 300, "15m": 900, "1h": 3600}


@dataclass
class OISnapshot:
    symbol: str
    open_interest: float
    oi_change_5m: float  # % change vs baseline ~5m ago
    oi_change_15m: float
    oi_change_1h: float
    price_change_pct: float
    oi_score: int  # +15, -10, or 0


def compute_oi_score(side: str, price_change_pct: float, oi_change_pct: float) -> int:
    """Return directional OI confluence score (+15, -10, or 0)."""
    price_up = price_change_pct > 0
    price_down = price_change_pct < 0
    oi_up = oi_change_pct > 0
    oi_down = oi_change_pct < 0

    if side == "LONG":
        if price_up and oi_up:
            return 15
        if price_up and oi_down:
            return -10
    elif side == "SHORT":
        if price_down and oi_up:
            return 15
        if price_down and oi_down:
            return -10
    return 0


async def _oi_change_for_window(symbol: str, current_oi: float, window: str) -> float:
    """
    Return % OI change for `window`.  Refreshes the Redis baseline once the
    window duration has elapsed so the comparison always stays fresh.
    Returns 0.0 on first observation or Redis miss (graceful cold-start).
    """
    key = f"oi:{symbol}:{window}"
    now = time.time()
    cached = await cache_get(key)

    if cached is None:
        await cache_set(key, {"oi": current_oi, "ts": now}, ttl=_TTL[window])
        return 0.0

    old_oi: float = float(cached.get("oi", 0.0))
    old_ts: float = float(cached.get("ts", 0.0))

    change_pct = ((current_oi - old_oi) / old_oi * 100.0) if old_oi > 0 else 0.0

    if (now - old_ts) >= _WINDOW_SEC[window]:
        await cache_set(key, {"oi": current_oi, "ts": now}, ttl=_TTL[window])

    return round(change_pct, 4)


async def fetch_oi_snapshot(
    symbol: str,
    price_change_pct: float,
    side: str,
) -> Optional[OISnapshot]:
    """
    Fetch current OI from Binance, compute rolling window changes from Redis,
    score directional confluence, and return an OISnapshot.

    Returns None on network / parse errors — callers degrade gracefully.
    """
    try:
        client = await get_client()
        raw = await client.open_interest(symbol)
        current_oi = float(raw["openInterest"])

        oi_5m, oi_15m, oi_1h = await asyncio.gather(
            _oi_change_for_window(symbol, current_oi, "5m"),
            _oi_change_for_window(symbol, current_oi, "15m"),
            _oi_change_for_window(symbol, current_oi, "1h"),
        )

        score = compute_oi_score(side, price_change_pct, oi_15m)

        return OISnapshot(
            symbol=symbol,
            open_interest=round(current_oi, 2),
            oi_change_5m=oi_5m,
            oi_change_15m=oi_15m,
            oi_change_1h=oi_1h,
            price_change_pct=round(price_change_pct, 4),
            oi_score=score,
        )
    except Exception as exc:
        logger.warning(f"OI fetch failed for {symbol}: {exc}")
        return None
