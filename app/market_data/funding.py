"""
Funding Rate Engine — Sprint 11B.

Fetches Binance USDT-M Futures funding rates, classifies crowd positioning,
and produces a contrarian directional score that feeds the confidence engine.

Core idea — funding reveals crowded trades:
  Very positive funding → longs are crowded → avoid LONG, prefer SHORT.
  Very negative funding → shorts are crowded → avoid SHORT, prefer LONG.
  Neutral funding       → no strong crowd bias → small positive nudge.

Score rules (applied to adjusted_confidence):
    LONG:  neutral=+5  negative=+8  extreme_negative=+10  positive=-5  extreme_positive=-15
    SHORT: neutral=+5  positive=+8  extreme_positive=+10  negative=-5  extreme_negative=-15
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.market_data.binance_client import get_client
from app.market_data.cache import cache_get, cache_set
from app.utils.logger import logger

# ── config helpers ─────────────────────────────────────────────────────────


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


def _get_thresholds() -> dict:
    return {
        "positive": _env_float("FUNDING_POSITIVE", 0.0003),
        "negative": _env_float("FUNDING_NEGATIVE", -0.0003),
        "extreme_positive": _env_float("FUNDING_EXTREME_POSITIVE", 0.0008),
        "extreme_negative": _env_float("FUNDING_EXTREME_NEGATIVE", -0.0008),
    }


_CACHE_TTL_KEY = "FUNDING_CACHE_SECONDS"
_BATCH_CACHE_KEY = "funding:batch"


# ── data types ─────────────────────────────────────────────────────────────


@dataclass
class FundingData:
    symbol: str
    funding_rate: float
    funding_time: Optional[int]  # Unix ms
    next_funding_time: Optional[int]  # Unix ms
    classification: str  # neutral/positive/negative/extreme_positive/extreme_negative


@dataclass
class FundingScore:
    classification: str
    score: int
    reason: str


# ── classification ─────────────────────────────────────────────────────────


def classify_funding(funding_rate: float) -> str:
    """
    Classify a funding rate into one of five buckets using env-configurable
    thresholds.  Falls back to safe defaults if env vars are missing.
    """
    t = _get_thresholds()
    if funding_rate >= t["extreme_positive"]:
        return "extreme_positive"
    if funding_rate >= t["positive"]:
        return "positive"
    if funding_rate <= t["extreme_negative"]:
        return "extreme_negative"
    if funding_rate <= t["negative"]:
        return "negative"
    return "neutral"


# ── scoring ────────────────────────────────────────────────────────────────

_SCORE_TABLE: Dict[str, Dict[str, int]] = {
    "LONG": {
        "neutral": 5,
        "negative": 8,
        "extreme_negative": 10,
        "positive": -5,
        "extreme_positive": -15,
    },
    "SHORT": {
        "neutral": 5,
        "positive": 8,
        "extreme_positive": 10,
        "negative": -5,
        "extreme_negative": -15,
    },
}

_REASON_TABLE: Dict[str, Dict[str, str]] = {
    "LONG": {
        "neutral": "Funding neutral; no crowd bias.",
        "negative": "Negative funding; contrarian bullish.",
        "extreme_negative": "Extreme negative funding; shorts very crowded.",
        "positive": "Positive funding; longs crowded; caution.",
        "extreme_positive": "Longs crowded; avoid chasing.",
    },
    "SHORT": {
        "neutral": "Funding neutral; no crowd bias.",
        "positive": "Positive funding; contrarian bearish.",
        "extreme_positive": "Extreme positive funding; longs very crowded.",
        "negative": "Negative funding; shorts crowded; caution.",
        "extreme_negative": "Shorts crowded; avoid chasing.",
    },
}


def score_funding_for_side(classification: str, side: str) -> FundingScore:
    """Return a FundingScore for a given funding classification and trade side."""
    side = side.upper()
    table = _SCORE_TABLE.get(side, {})
    score = table.get(classification, 0)
    reasons = _REASON_TABLE.get(side, {})
    reason = reasons.get(classification, "")
    return FundingScore(classification=classification, score=score, reason=reason)


# ── fetch helpers ──────────────────────────────────────────────────────────


def _cache_ttl() -> int:
    try:
        return int(os.getenv(_CACHE_TTL_KEY, "300"))
    except Exception:
        return 300


def _parse_funding_entry(entry: dict) -> Optional[FundingData]:
    try:
        symbol = entry.get("symbol", "")
        rate = float(entry.get("lastFundingRate", 0) or 0)
        return FundingData(
            symbol=symbol,
            funding_rate=rate,
            funding_time=entry.get("time"),
            next_funding_time=entry.get("nextFundingTime"),
            classification=classify_funding(rate),
        )
    except Exception:
        return None


async def _fetch_batch_raw() -> List[dict]:
    """Fetch all symbols' premium index data from Binance (no symbol filter)."""
    client = await get_client()
    data = await client.premium_index()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


async def fetch_funding_rates(symbols: List[str]) -> Dict[str, FundingData]:
    """
    Batch-fetch funding rates for the given symbols.

    Uses a shared Redis cache keyed at _BATCH_CACHE_KEY with TTL=FUNDING_CACHE_SECONDS
    so the full universe fetch is reused across concurrent symbol analyses.
    Returns a dict mapping symbol → FundingData (only for requested symbols).
    """
    ttl = _cache_ttl()

    cached = await cache_get(_BATCH_CACHE_KEY)
    if cached is None:
        try:
            raw_list = await _fetch_batch_raw()
            cached = {r["symbol"]: r for r in raw_list if "symbol" in r}
            await cache_set(_BATCH_CACHE_KEY, cached, ttl=ttl)
        except Exception as exc:
            logger.warning(f"funding batch fetch failed: {exc}")
            return {}

    result: Dict[str, FundingData] = {}
    for sym in symbols:
        entry = cached.get(sym)
        if entry:
            fd = _parse_funding_entry(entry)
            if fd:
                result[sym] = fd
    return result


async def fetch_funding_rate(symbol: str) -> Optional[FundingData]:
    """
    Fetch funding rate for a single symbol.
    Tries the batch cache first to avoid redundant API calls.
    Falls back to a direct single-symbol request on miss.
    """
    cached = await cache_get(_BATCH_CACHE_KEY)
    if cached:
        entry = cached.get(symbol)
        if entry:
            return _parse_funding_entry(entry)

    try:
        client = await get_client()
        raw = await client.premium_index(symbol=symbol)
        if isinstance(raw, list) and raw:
            raw = raw[0]
        return _parse_funding_entry(raw)
    except Exception as exc:
        logger.warning(f"funding fetch failed for {symbol}: {exc}")
        return None
