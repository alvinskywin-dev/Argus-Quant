"""
Market Regime Engine — Sprint 19A.

Classifies overall market conditions before signals are generated:
    BULL           — BTC/ETH above EMA200, breadth > 60%
    BEAR           — BTC/ETH below EMA200, breadth < 40%
    SIDEWAYS       — Mixed trend, ATR near median
    HIGH_VOLATILITY — ATR percentile > 80
    LOW_VOLATILITY  — ATR percentile < 20

Results are cached in Redis for 10 minutes and refreshed once per scan cycle.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Dict, Optional, Tuple

import numpy as np

from app.indicators.ta import atr, ema
from app.market_data.cache import cache_get, cache_set
from app.market_data.klines import fetch_klines
from app.market_data.universe import universe
from app.utils.helpers import utcnow
from app.utils.logger import logger

_CACHE_KEY = "market:regime:v1"
_CACHE_TTL = 600  # 10 minutes — regime is stable within a scan cycle


@dataclass
class MarketRegime:
    market_regime: str    # BULL | BEAR | SIDEWAYS | HIGH_VOLATILITY | LOW_VOLATILITY
    regime_score: int     # 0-100 (50 = neutral)
    breadth_ema200: float # % of USDT pairs above EMA200
    breadth_ema50: float  # % of USDT pairs above EMA50
    btc_trend: str        # UP | DOWN | NEUTRAL
    eth_trend: str        # UP | DOWN | NEUTRAL
    atr_percentile: float # BTC 1D ATR vs 90-bar history (0-100)
    calculated_at: str    # ISO8601 UTC


# ── trend helpers ─────────────────────────────────────────────────────────────

async def _get_trend(symbol: str, tf: str) -> str:
    """Return UP / DOWN / NEUTRAL based on close vs EMA200."""
    try:
        df = await fetch_klines(symbol, tf, limit=220)
        if df is None or df.empty or len(df) < 201:
            return "NEUTRAL"
        close = df["close"]
        ema200_val = float(ema(close, 200).iloc[-1])
        last_close = float(close.iloc[-1])
        if last_close > ema200_val * 1.001:
            return "UP"
        if last_close < ema200_val * 0.999:
            return "DOWN"
        return "NEUTRAL"
    except Exception as exc:
        logger.debug(f"regime trend fetch failed {symbol}/{tf}: {exc}")
        return "NEUTRAL"


# ── ATR percentile ─────────────────────────────────────────────────────────────

async def _get_atr_percentile(symbol: str = "BTCUSDT") -> float:
    """Current BTC 1D ATR as a percentile of the last 90 bars (0-100)."""
    try:
        df = await fetch_klines(symbol, "1d", limit=150)
        if df is None or df.empty or len(df) < 20:
            return 50.0
        atr_series = atr(df, 14).dropna()
        if len(atr_series) < 10:
            return 50.0
        current = float(atr_series.iloc[-1])
        history = atr_series.values
        pct = float(np.mean(history <= current) * 100)
        return round(pct, 1)
    except Exception as exc:
        logger.debug(f"regime ATR percentile failed: {exc}")
        return 50.0


# ── breadth calculation ───────────────────────────────────────────────────────

async def _get_breadth(max_symbols: int = 50) -> Dict[str, float]:
    """
    Estimate % of USDT pairs above EMA200 and EMA50 on the 1D chart.
    Samples the top `max_symbols` by volume to keep API usage bounded.
    1D klines have a 3600s Redis TTL so repeated calls within an hour are free.
    """
    symbols = universe.symbols[:max_symbols]
    if not symbols:
        return {"breadth_ema200": 50.0, "breadth_ema50": 50.0}

    sem = asyncio.Semaphore(15)

    async def check_one(sym: str) -> Optional[Dict[str, bool]]:
        async with sem:
            try:
                df = await fetch_klines(sym, "1d", limit=210)
                if df is None or df.empty or len(df) < 201:
                    return None
                close = df["close"]
                last_close = float(close.iloc[-1])
                ema200_val = float(ema(close, 200).iloc[-1])
                ema50_val = float(ema(close, 50).iloc[-1])
                return {
                    "above_ema200": last_close > ema200_val,
                    "above_ema50": last_close > ema50_val,
                }
            except Exception:
                return None

    results = await asyncio.gather(*[check_one(s) for s in symbols])
    valid = [r for r in results if r is not None]

    if not valid:
        return {"breadth_ema200": 50.0, "breadth_ema50": 50.0}

    n = len(valid)
    above200 = sum(1 for r in valid if r["above_ema200"])
    above50 = sum(1 for r in valid if r["above_ema50"])
    return {
        "breadth_ema200": round(above200 / n * 100, 1),
        "breadth_ema50": round(above50 / n * 100, 1),
    }


# ── scoring & classification ──────────────────────────────────────────────────

def _classify_regime(
    btc_1d: str,
    eth_1d: str,
    btc_4h: str,
    eth_4h: str,
    btc_1h: str,
    breadth_ema200: float,
    atr_percentile: float,
) -> Tuple[str, int]:
    """Return (regime_label, score 0-100) based on all inputs."""
    score = 50  # neutral baseline

    # 1D trend: strongest directional signal  (±20)
    if btc_1d == "UP":     score += 12
    elif btc_1d == "DOWN": score -= 12

    if eth_1d == "UP":     score += 8
    elif eth_1d == "DOWN": score -= 8

    # 4H trend: medium-term momentum  (±10)
    if btc_4h == "UP":     score += 6
    elif btc_4h == "DOWN": score -= 6

    if eth_4h == "UP":     score += 4
    elif eth_4h == "DOWN": score -= 4

    # 1H trend: short-term bias  (±3)
    if btc_1h == "UP":     score += 3
    elif btc_1h == "DOWN": score -= 3

    # Market breadth  (±15)
    if breadth_ema200 > 70:   score += 10
    elif breadth_ema200 > 60: score += 5
    elif breadth_ema200 < 30: score -= 10
    elif breadth_ema200 < 40: score -= 5

    score = max(0, min(100, score))

    # Volatility overrides trend classification
    if atr_percentile > 80:
        return "HIGH_VOLATILITY", score
    if atr_percentile < 20:
        return "LOW_VOLATILITY", score

    if score >= 62:
        return "BULL", score
    if score <= 38:
        return "BEAR", score
    return "SIDEWAYS", score


# ── public API ────────────────────────────────────────────────────────────────

async def calculate_market_regime() -> MarketRegime:
    """
    Full regime calculation. Fetches BTC/ETH multi-TF trends, market breadth,
    and BTC ATR percentile, then classifies and caches the result.
    """
    (
        btc_1d, eth_1d,
        btc_4h, eth_4h,
        btc_1h,
        atr_pct,
        breadth,
    ) = await asyncio.gather(
        _get_trend("BTCUSDT", "1d"),
        _get_trend("ETHUSDT", "1d"),
        _get_trend("BTCUSDT", "4h"),
        _get_trend("ETHUSDT", "4h"),
        _get_trend("BTCUSDT", "1h"),
        _get_atr_percentile("BTCUSDT"),
        _get_breadth(50),
    )

    breadth_200 = breadth["breadth_ema200"]
    breadth_50 = breadth["breadth_ema50"]

    regime_label, score = _classify_regime(
        btc_1d, eth_1d,
        btc_4h, eth_4h,
        btc_1h,
        breadth_200,
        atr_pct,
    )

    result = MarketRegime(
        market_regime=regime_label,
        regime_score=score,
        breadth_ema200=breadth_200,
        breadth_ema50=breadth_50,
        btc_trend=btc_1d,
        eth_trend=eth_1d,
        atr_percentile=atr_pct,
        calculated_at=utcnow().isoformat(),
    )

    await cache_set(_CACHE_KEY, asdict(result), ttl=_CACHE_TTL)
    logger.info(
        f"🌍 Market regime: {regime_label} (score={score}) "
        f"BTC={btc_1d} ETH={eth_1d} "
        f"breadth={breadth_200}% ATR_pct={atr_pct}"
    )
    return result


async def get_market_regime() -> Optional[MarketRegime]:
    """Return cached market regime, or None if not yet calculated."""
    cached = await cache_get(_CACHE_KEY)
    if cached:
        try:
            return MarketRegime(**cached)
        except Exception:
            return None
    return None


async def ensure_regime_fresh() -> MarketRegime:
    """
    Return cached regime if still valid, else recalculate.
    Call once at the start of each scan cycle to warm the cache.
    """
    cached = await cache_get(_CACHE_KEY)
    if cached:
        try:
            return MarketRegime(**cached)
        except Exception:
            pass
    return await calculate_market_regime()
