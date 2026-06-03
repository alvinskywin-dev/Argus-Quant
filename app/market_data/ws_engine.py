from __future__ import annotations

import asyncio
import time

import aiohttp

from app.utils.logger import logger

# Latest realtime mark price per symbol (USDT-M futures, last trade price).
latest_prices: dict[str, float] = {}
# Wall-clock epoch seconds of the last successful update for each symbol.
price_updated_at: dict[str, float] = {}
price_history: dict[str, list[tuple[float, float]]] = {}
last_ws_update = 0.0

# Default symbols that are always polled (drive the market-bias widget). Any
# symbol with an open position is registered on top of these at runtime so the
# paper/live engines always have a live mark — never a stale or entry fallback.
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
_tracked: set[str] = set(DEFAULT_SYMBOLS)

# A cached price older than this is considered stale and refetched on demand.
STALE_AFTER_SEC = 5.0

PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"

# Reconnect/backoff tuning for the price-poll loop.
POLL_INTERVAL_SEC = 2.0  # steady-state cadence between successful polls
BACKOFF_BASE_SEC = 2.0  # first delay after a loop-level failure
BACKOFF_MAX_SEC = 60.0  # cap so a long outage still retries once a minute


def register_symbols(symbols) -> None:
    """Add symbols to the polled set so the loop keeps their price fresh."""
    for s in symbols:
        if s:
            _tracked.add(s.upper())


def price_age(symbol: str) -> float | None:
    """Seconds since `symbol`'s price was last refreshed, or None if never."""
    ts = price_updated_at.get(symbol)
    return (time.time() - ts) if ts else None


def _store(symbol: str, price: float) -> None:
    now = time.time()
    latest_prices[symbol] = price
    price_updated_at[symbol] = now
    hist = price_history.setdefault(symbol, [])
    hist.append((now, price))
    cutoff = now - 300
    price_history[symbol] = [(t, p) for t, p in hist if t >= cutoff]


async def _fetch(session: aiohttp.ClientSession, symbol: str) -> float | None:
    async with session.get(PRICE_URL, params={"symbol": symbol}, timeout=10) as r:
        data = await r.json()
    if isinstance(data, dict) and "price" in data:
        return float(data["price"])
    return None


async def fetch_price(symbol: str) -> float | None:
    """Fetch one symbol's live price now, cache it, and start tracking it."""
    symbol = symbol.upper()
    register_symbols([symbol])
    try:
        async with aiohttp.ClientSession() as session:
            price = await _fetch(session, symbol)
        if price and price > 0:
            _store(symbol, price)
            return price
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"on-demand price fetch failed {symbol}: {exc}")
    return None


async def ensure_prices(symbols) -> None:
    """
    Guarantee a fresh cached price for each symbol. Registers them for ongoing
    polling and synchronously fetches any that are missing or stale, so callers
    never have to fall back to entry price for a mark.
    """
    wanted = {s.upper() for s in symbols if s}
    if not wanted:
        return
    register_symbols(wanted)
    missing = [
        s for s in wanted if s not in latest_prices or (price_age(s) or 1e9) > STALE_AFTER_SEC
    ]
    if not missing:
        return
    try:
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(
                *(_fetch(session, s) for s in missing), return_exceptions=True
            )
        for s, price in zip(missing, results, strict=True):
            if isinstance(price, (int, float)) and price and price > 0:
                _store(s, float(price))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"ensure_prices failed for {missing}: {exc}")


async def ws_price_loop() -> None:
    global last_ws_update

    backoff = BACKOFF_BASE_SEC

    while True:
        try:
            symbols = sorted(_tracked)
            async with aiohttp.ClientSession() as session:
                for symbol in symbols:
                    try:
                        price = await _fetch(session, symbol)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"price poll miss {symbol}: {exc}")
                        continue
                    if price and price > 0:
                        _store(symbol, price)
                        last_ws_update = time.time()

            logger.info(f"realtime price cache updated: {len(latest_prices)} symbols")

            # Healthy cycle — reset backoff and resume steady cadence.
            backoff = BACKOFF_BASE_SEC
            await asyncio.sleep(POLL_INTERVAL_SEC)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Loop-level failure (e.g. session/DNS outage): back off
            # exponentially with a cap so we neither hammer Binance nor stop
            # retrying during a prolonged outage.
            logger.warning(f"price cache error: {exc} — retrying in {backoff:.0f}s")
            _record_reconnect()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SEC)


def _record_reconnect() -> None:
    """Best-effort metric bump for a loop-level price-feed reconnect."""
    try:
        from app.utils.observability import METRICS

        METRICS.inc_ws_reconnect()
    except Exception:  # noqa: BLE001 — metrics must never break the loop
        pass


def ws_health() -> dict:
    age = time.time() - last_ws_update if last_ws_update else None

    return {
        "ok": bool(last_ws_update and age is not None and age < 10),
        "last_update_age_sec": round(age, 2) if age is not None else None,
        "tracked_symbols": len(_tracked),
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
