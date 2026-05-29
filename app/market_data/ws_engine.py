from __future__ import annotations

import asyncio
import time

import aiohttp

from app.utils.logger import logger


latest_prices = {}
price_history = {}
last_ws_update = 0.0

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


async def ws_price_loop() -> None:
    global last_ws_update

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                for symbol in SYMBOLS:
                    url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={symbol}"

                    async with session.get(url, timeout=10) as r:
                        data = await r.json()

                    if "price" in data:
                        price = float(data["price"])
                        latest_prices[symbol] = price

                        hist = price_history.setdefault(symbol, [])
                        hist.append((time.time(), price))

                        cutoff = time.time() - 300
                        price_history[symbol] = [(t, p) for t, p in hist if t >= cutoff]

                        last_ws_update = time.time()

            logger.info(f"realtime price cache updated: {latest_prices}")

        except Exception as exc:
            logger.warning(f"price cache error: {exc}")

        await asyncio.sleep(2)


def ws_health() -> dict:
    age = time.time() - last_ws_update if last_ws_update else None

    return {
        "ok": bool(last_ws_update and age is not None and age < 10),
        "last_update_age_sec": round(age, 2) if age is not None else None,
        "prices": latest_prices,
        "market_bias": market_bias(),
    }


def _pct_change(symbol: str) -> float:
    hist = price_history.get(symbol, [])
    if len(hist) < 2:
        return 0.0

    first = hist[0][1]
    last = hist[-1][1]

    if not first:
        return 0.0

    return ((last - first) / first) * 100


def market_bias() -> dict:
    btc = _pct_change("BTCUSDT")
    eth = _pct_change("ETHUSDT")
    sol = _pct_change("SOLUSDT")

    avg = (btc + eth + sol) / 3

    if avg >= 0.25:
        bias = "RISK_ON"
    elif avg <= -0.25:
        bias = "RISK_OFF"
    else:
        bias = "NEUTRAL"

    return {
        "bias": bias,
        "avg_5m_change_pct": round(avg, 3),
        "btc_5m_change_pct": round(btc, 3),
        "eth_5m_change_pct": round(eth, 3),
        "sol_5m_change_pct": round(sol, 3),
    }
