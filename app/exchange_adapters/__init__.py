"""
Sprint 20F — exchange adapter factory.

resolve_adapter() is the single chokepoint that decides MOCK vs LIVE. A real
adapter is returned ONLY when the live-trading gate is fully open:

    runtime live switch == ON  AND  MOCK_EXCHANGE_MODE == false

The runtime switch is toggled by an admin at runtime (persisted in
system_settings, loaded at startup); MOCK_EXCHANGE_MODE stays the hard env floor.
In every other case (the default), a MockExchangeAdapter is returned and no real
order can be placed. 20G registers OKX/Bybit/Bitget here.
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


# ── Runtime live-trading switch ───────────────────────────────────────────────
# An admin can flip real trading on/off at runtime (no restart) via the admin API;
# the value is persisted in system_settings and loaded at startup. This in-memory
# mirror lets the sync hot-path gate read it without a DB call. MOCK_EXCHANGE_MODE
# stays the hard env floor: when it is true the gate is closed regardless.
# Seeded from LIVE_TRADING_ENABLED for back-compat until startup loads the DB value.
LIVE_TRADING_RUNTIME_KEY = "live_trading_runtime_enabled"
_RUNTIME_LIVE_ENABLED: bool = bool(settings.live_trading_enabled)


async def load_runtime_live_enabled() -> None:
    """Load the persisted runtime switch from system_settings at startup. Falls
    back to the seeded LIVE_TRADING_ENABLED default when no row exists yet."""
    from app.database import repo

    val = await repo.get_setting(LIVE_TRADING_RUNTIME_KEY, None)
    if val is not None:
        set_runtime_live_enabled(val.strip().lower() in ("1", "true", "yes", "on"))


def runtime_live_enabled() -> bool:
    return _RUNTIME_LIVE_ENABLED


def set_runtime_live_enabled(value: bool) -> None:
    """Update the in-memory runtime switch (called by the admin toggle + startup)."""
    global _RUNTIME_LIVE_ENABLED
    _RUNTIME_LIVE_ENABLED = bool(value)


def live_gate_open() -> bool:
    """True only when real orders are permitted: the admin runtime switch is on
    AND the hard env floor (MOCK_EXCHANGE_MODE=false) allows it."""
    return bool(_RUNTIME_LIVE_ENABLED and not settings.mock_exchange_mode)


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
