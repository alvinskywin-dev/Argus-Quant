"""
Maintains the active USDT-M Futures symbol universe. Filters out:
    - non USDT pairs
    - non TRADING status
    - low-volume symbols (configurable threshold)

Refreshes on a timer.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List

from app.config import settings
from app.market_data.binance_client import get_client
from app.utils.logger import logger


class SymbolUniverse:
    def __init__(self) -> None:
        self.symbols: List[str] = []
        self.meta: Dict[str, dict] = {}  # symbol -> dict(price, vol, change_pct, ...)
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        client = await get_client()
        async with self._lock:
            try:
                info = await client.exchange_info()
                tickers = await client.ticker_24h()

                stable_bases = settings.stablecoin_base_set
                allowed = {
                    s["symbol"]
                    for s in info.get("symbols", [])
                    if s.get("status") == "TRADING"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("quoteAsset") == "USDT"
                    and s.get("baseAsset", "").upper() not in stable_bases
                }

                meta: Dict[str, dict] = {}
                for t in tickers:
                    sym = t["symbol"]
                    if sym not in allowed:
                        continue
                    vol = float(t.get("quoteVolume", 0.0))
                    if vol < settings.min_quote_volume_usdt:
                        continue
                    meta[sym] = {
                        "price": float(t["lastPrice"]),
                        "change_pct": float(t["priceChangePercent"]),
                        "quote_volume": vol,
                        "high": float(t["highPrice"]),
                        "low": float(t["lowPrice"]),
                    }

                # sort by quote_volume desc — most liquid first
                ordered = sorted(meta.keys(), key=lambda s: -meta[s]["quote_volume"])
                if settings.max_symbols > 0:
                    ordered = ordered[: settings.max_symbols]

                self.symbols = ordered
                self.meta = {s: meta[s] for s in ordered}
                logger.info(f"universe refreshed: {len(self.symbols)} symbols tradable")
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"universe refresh failed: {exc}")

    def gainers(self, n: int = 10) -> List[dict]:
        items = [{"symbol": s, **m} for s, m in self.meta.items()]
        items.sort(key=lambda x: -x["change_pct"])
        return items[:n]

    def losers(self, n: int = 10) -> List[dict]:
        items = [{"symbol": s, **m} for s, m in self.meta.items()]
        items.sort(key=lambda x: x["change_pct"])
        return items[:n]


universe = SymbolUniverse()


async def universe_loop() -> None:
    """Periodic refresher — run as background task."""
    while True:
        await universe.refresh()
        await asyncio.sleep(settings.universe_refresh_sec)
