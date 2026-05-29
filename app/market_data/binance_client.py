"""
Async Binance USDT-M Futures client built on aiohttp.

Why custom and not python-binance?
- python-binance ships sync + async, but it bundles a lot we don't need.
- We need fine-grained rate-limit-aware control, retries, and clean asyncio
  semantics. A focused custom client is easier to keep production-grade.

Endpoints used:
    GET /fapi/v1/exchangeInfo       — symbol universe
    GET /fapi/v1/ticker/24hr        — 24h ticker (volume, change)
    GET /fapi/v1/klines             — OHLCV
    GET /fapi/v1/premiumIndex       — mark price + funding
    GET /fapi/v1/openInterest       — open interest
    GET /futures/data/topLongShortAccountRatio (data API)
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.utils.logger import logger

BASE_URL = "https://fapi.binance.com" if not settings.binance_testnet else "https://testnet.binancefuture.com"
DATA_URL = "https://fapi.binance.com"  # data endpoints only live on prod


class BinanceError(Exception):
    pass


class BinanceClient:
    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        # Binance allows 2400 weight/min on REST. We throttle conservatively.
        self._semaphore = asyncio.Semaphore(20)

    async def __aenter__(self) -> "BinanceClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        base: str = BASE_URL,
    ) -> Any:
        await self.start()
        url = f"{base}{path}"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError, BinanceError)),
            reraise=True,
        ):
            with attempt:
                async with self._semaphore:
                    async with self._session.get(url, params=params) as resp:  # type: ignore[union-attr]
                        if resp.status == 429 or resp.status == 418:
                            retry_after = int(resp.headers.get("Retry-After", "1"))
                            logger.warning(f"binance rate limit hit, sleeping {retry_after}s")
                            await asyncio.sleep(retry_after)
                            raise BinanceError("rate-limited")
                        if resp.status >= 500:
                            raise BinanceError(f"binance 5xx: {resp.status}")
                        if resp.status >= 400:
                            txt = await resp.text()
                            raise BinanceError(f"binance {resp.status}: {txt[:200]}")
                        return await resp.json()

    # ---------- public endpoints ----------
    async def exchange_info(self) -> Dict[str, Any]:
        return await self._get("/fapi/v1/exchangeInfo")

    async def ticker_24h(self) -> List[Dict[str, Any]]:
        return await self._get("/fapi/v1/ticker/24hr")

    async def klines(
        self, symbol: str, interval: str, limit: int = 200
    ) -> List[List[Any]]:
        return await self._get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )

    async def premium_index(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return await self._get("/fapi/v1/premiumIndex", params=params)

    async def open_interest(self, symbol: str) -> Dict[str, Any]:
        return await self._get("/fapi/v1/openInterest", params={"symbol": symbol})

    async def long_short_ratio(
        self, symbol: str, period: str = "5m", limit: int = 5
    ) -> List[Dict[str, Any]]:
        return await self._get(
            "/futures/data/topLongShortAccountRatio",
            params={"symbol": symbol, "period": period, "limit": limit},
            base=DATA_URL,
        )

    async def book_ticker(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return await self._get("/fapi/v1/ticker/bookTicker", params=params)


# Module-level singleton — convenient for scanner code
_client: Optional[BinanceClient] = None


async def get_client() -> BinanceClient:
    global _client
    if _client is None:
        _client = BinanceClient()
        await _client.start()
    return _client


async def shutdown_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
