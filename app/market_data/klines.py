"""
Async OHLCV fetcher with redis caching. Returns pandas DataFrame.

Cache TTL is short (a fraction of the timeframe) so the data stays fresh but
we don't slam the REST API.
"""

from __future__ import annotations

import pandas as pd

from app.market_data.binance_client import get_client
from app.market_data.cache import cache_get, cache_set
from app.utils.logger import logger

# rough cache TTL per timeframe (sec)
_TTL: dict[str, int] = {
    "1m": 15,
    "5m": 60,
    "15m": 180,
    "30m": 300,
    "1h": 600,
    "4h": 1800,
    "1d": 3600,
}


def _to_df(rows: list[list]) -> pd.DataFrame:
    cols = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "trades",
        "taker_base_vol",
        "taker_quote_vol",
        "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.dropna()
    return df


async def fetch_klines_historical(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> pd.DataFrame:
    """
    Fetch all historical klines for a ms-timestamp range via batched REST calls.
    No caching — intended for backtest data ingestion only.
    """
    client = await get_client()
    try:
        rows = await client.klines_range(symbol, interval, start_ms, end_ms)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"klines_range failed {symbol} {interval}: {exc}")
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    return _to_df(rows)


async def fetch_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
    key = f"kl:{symbol}:{interval}:{limit}"
    cached = await cache_get(key)
    if cached:
        return _to_df(cached)

    client = await get_client()
    try:
        rows = await client.klines(symbol, interval, limit=limit)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"klines fetch failed {symbol} {interval}: {exc}")
        return pd.DataFrame()

    ttl = _TTL.get(interval, 60)
    await cache_set(key, rows, ttl=ttl)
    return _to_df(rows)
