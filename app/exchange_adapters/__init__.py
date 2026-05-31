"""
Sprint 20F — exchange adapter factory.

resolve_adapter() is the single chokepoint that decides MOCK vs LIVE. A real
adapter is returned ONLY when the live-trading gate is fully open:

    LIVE_TRADING_ENABLED == true  AND  MOCK_EXCHANGE_MODE == false

In every other case (the default), a MockExchangeAdapter is returned and no
real order can be placed. 20G registers OKX/Bybit/Bitget here.
"""
from __future__ import annotations

from typing import Optional

from app.config import settings
from app.exchange_adapters.base import ExchangeAdapter
from app.exchange_adapters.mock import MockExchangeAdapter

# Real (LIVE) adapters by exchange. 20G adds okx/bybit/bitget.
_LIVE_ADAPTERS = {"binance"}

SUPPORTED_EXCHANGES = ("binance", "okx", "bybit", "bitget")


def live_gate_open() -> bool:
    """True only when real orders are permitted."""
    return bool(settings.live_trading_enabled and not settings.mock_exchange_mode)


def resolve_adapter(
    exchange: str,
    *,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    passphrase: Optional[str] = None,
) -> ExchangeAdapter:
    """Return a LIVE adapter only if the gate is open AND creds are supplied."""
    exchange = exchange.lower()

    if live_gate_open() and exchange in _LIVE_ADAPTERS and api_key and api_secret:
        if exchange == "binance":
            from app.exchange_adapters.binance import BinanceFuturesAdapter
            return BinanceFuturesAdapter(api_key, api_secret)

    # Default, safe path: simulate everything.
    return MockExchangeAdapter(exchange)
