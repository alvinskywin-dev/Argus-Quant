"""
Phase 21F — Binance Futures testnet configuration + safety guards.

A pure, testable layer the testnet preflight script depends on. It resolves the
dedicated testnet credentials/URLs from settings and *refuses* (raises
BinanceTestnetGuardError) whenever the configuration is missing, disabled, or points at
anything other than the Binance futures testnet host. This is what keeps the
"validate against testnet, never mainnet, no real money" guarantee enforceable
in unit tests — the network and any order placement live in the script and are
gated separately behind an explicit CLI flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.exchange_vault.binance_preflight import _TESTNET_FAPI

# The only acceptable REST host in testnet mode; mainnet is fapi.binance.com.
TESTNET_HOST = "testnet.binancefuture.com"
MAINNET_HOST = "fapi.binance.com"
DEFAULT_TESTNET_BASE_URL = _TESTNET_FAPI
DEFAULT_TESTNET_WS_URL = "wss://stream.binancefuture.com/ws"


class BinanceTestnetGuardError(Exception):
    """The testnet preflight must refuse to run (misconfigured or unsafe)."""


@dataclass
class BinanceTestnetConfig:
    base_url: str
    ws_url: str
    api_key: str
    api_secret: str
    mode: str = "TESTNET"


def is_testnet_url(url: str) -> bool:
    """True only for the Binance futures *testnet* host, never mainnet."""
    u = (url or "").lower()
    return TESTNET_HOST in u and MAINNET_HOST not in u


def resolve_testnet_config(
    *, api_key: Optional[str] = None, api_secret: Optional[str] = None
) -> BinanceTestnetConfig:
    """Build and validate the testnet config from settings (with optional
    explicit key/secret overrides). Raises BinanceTestnetGuardError — never returns a
    mainnet or partially-configured config.

    Order of refusal: feature flag off → non-testnet base URL → missing keys.
    """
    if not settings.binance_testnet_enabled:
        raise BinanceTestnetGuardError("BINANCE_TESTNET_ENABLED is false — refusing to run")

    base = (settings.binance_testnet_base_url or DEFAULT_TESTNET_BASE_URL).strip()
    if not is_testnet_url(base):
        raise BinanceTestnetGuardError(f"refusing non-testnet base URL: {base!r}")

    key = (api_key if api_key is not None else settings.binance_testnet_api_key) or ""
    secret = (api_secret if api_secret is not None else settings.binance_testnet_api_secret) or ""
    if not key or not secret:
        raise BinanceTestnetGuardError(
            "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET not set"
        )

    ws = (settings.binance_testnet_ws_url or DEFAULT_TESTNET_WS_URL).strip()
    return BinanceTestnetConfig(
        base_url=base, ws_url=ws, api_key=key, api_secret=secret, mode="TESTNET"
    )
