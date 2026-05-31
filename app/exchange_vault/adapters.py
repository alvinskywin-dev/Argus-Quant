"""
Sprint 20C — exchange credential validation adapters.

A thin interface used at connect/test time to verify an API key's permissions.
Real network adapters (Binance/OKX/Bybit/Bitget) arrive in Sprint 20F/20G; in
MOCK_EXCHANGE_MODE (the default) no network call is made — validation is
simulated deterministically so both the accept and the reject (withdrawal-
enabled) paths are testable without real keys or risk.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

SUPPORTED_EXCHANGES = ("binance", "okx", "bybit", "bitget")
# Exchanges that additionally require a passphrase.
PASSPHRASE_EXCHANGES = ("okx", "bitget")


@dataclass
class Permissions:
    valid: bool
    can_trade: bool = False
    can_futures: bool = False
    can_withdraw: bool = False
    account_type: str = "futures"
    message: str = ""


class ExchangeValidator:
    """Base interface: validate(...) -> Permissions."""

    name = "base"

    def validate(self, api_key: str, api_secret: str, passphrase: str | None) -> Permissions:
        raise NotImplementedError


class MockExchangeValidator(ExchangeValidator):
    """
    Deterministic offline validator. Permission flags are inferred from the
    api_key string so tests (and demos) can drive every branch:

      contains "WITHDRAW" or starts with "WD"  -> withdrawal enabled (rejected)
      contains "NOTRADE"                        -> trading disabled
      contains "NOFUTURES"                      -> futures disabled
      contains "BADKEY"                         -> invalid credentials
      otherwise                                 -> trade + futures, no withdrawal
    """

    name = "mock"

    def __init__(self, exchange: str):
        self.exchange = exchange

    def validate(self, api_key: str, api_secret: str, passphrase: str | None) -> Permissions:
        if not api_key or not api_secret:
            return Permissions(valid=False, message="Missing API key or secret")
        if self.exchange in PASSPHRASE_EXCHANGES and not passphrase:
            return Permissions(valid=False, message=f"{self.exchange} requires a passphrase")

        up = api_key.upper()
        if "BADKEY" in up:
            return Permissions(valid=False, message="Invalid API credentials")

        can_withdraw = "WITHDRAW" in up or up.startswith("WD")
        can_trade = "NOTRADE" not in up
        can_futures = "NOFUTURES" not in up
        return Permissions(
            valid=True,
            can_trade=can_trade,
            can_futures=can_futures,
            can_withdraw=can_withdraw,
            account_type="futures",
            message="validated (mock)",
        )


def get_validator(exchange: str) -> ExchangeValidator:
    """Return the validator for `exchange`, honoring MOCK_EXCHANGE_MODE."""
    exchange = exchange.lower()
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange}")
    if settings.mock_exchange_mode:
        return MockExchangeValidator(exchange)
    # Real adapters are introduced in Sprint 20F/20G.
    raise NotImplementedError(
        f"Live validation for {exchange} is not available yet "
        f"(MOCK_EXCHANGE_MODE is false but no real adapter is installed)."
    )
