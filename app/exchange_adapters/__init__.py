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

# Real (LIVE) adapters by exchange. Future-ready: hyperliquid/mexc/gate/kucoin.
_LIVE_ADAPTERS = {"binance", "okx", "bybit", "bitget"}
# Exchanges whose credentials include a passphrase.
PASSPHRASE_EXCHANGES = ("okx", "bitget")

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
    """
    Return a LIVE adapter only if the gate is open AND creds are supplied;
    otherwise a MockExchangeAdapter. This is the auto-routing target: callers
    pass the exchange chosen for the signal and get the right adapter back.
    """
    exchange = exchange.lower()

    if live_gate_open() and exchange in _LIVE_ADAPTERS and api_key and api_secret:
        if exchange == "binance":
            from app.exchange_adapters.binance import BinanceFuturesAdapter
            return BinanceFuturesAdapter(api_key, api_secret)
        if exchange == "okx":
            from app.exchange_adapters.okx import OKXAdapter
            return OKXAdapter(api_key, api_secret, passphrase or "")
        if exchange == "bybit":
            from app.exchange_adapters.bybit import BybitAdapter
            return BybitAdapter(api_key, api_secret)
        if exchange == "bitget":
            from app.exchange_adapters.bitget import BitgetAdapter
            return BitgetAdapter(api_key, api_secret, passphrase or "")

    # Default, safe path: simulate everything.
    return MockExchangeAdapter(exchange)
