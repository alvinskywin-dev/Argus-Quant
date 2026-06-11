"""
Sprint 20F — unified exchange adapter interface.

Defines the common contract every exchange adapter implements (Binance here in
20F; OKX/Bybit/Bitget added in 20G). Results carry a `mode` field that is
"MOCK" or "LIVE" so every layer above can see, unambiguously, whether a real
order was placed.

SAFETY: real adapters must place no real orders unless the live-trading gate
is open (LIVE_TRADING_ENABLED=true AND MOCK_EXCHANGE_MODE=false). The gate is
enforced centrally in app.live_trading.service.resolve_adapter and again,
defensively, inside each real adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

MODE_MOCK = "MOCK"
MODE_LIVE = "LIVE"


class AdapterError(Exception):
    """An exchange call failed (network, signature, rejection, etc.)."""


class AdapterTimeoutError(AdapterError):
    """A request timed out or the connection dropped mid-flight.

    Distinct from AdapterError because the outcome is *ambiguous*: the order may
    or may not have reached the exchange. Callers must resolve the real state
    (e.g. via get_order_by_client_id) rather than assuming the order failed.
    """


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str  # BUY / SELL
    type: str  # MARKET / LIMIT / STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET
    status: str  # NEW / FILLED / PARTIALLY_FILLED / CANCELED / REJECTED
    price: float = 0.0
    qty: float = 0.0
    filled_qty: float = 0.0
    avg_price: float = 0.0
    reduce_only: bool = False
    mode: str = MODE_MOCK
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BalanceInfo:
    asset: str
    balance: float
    available: float
    mode: str = MODE_MOCK


@dataclass
class PositionInfo:
    symbol: str
    side: str  # LONG / SHORT / FLAT
    qty: float
    entry_price: float
    leverage: int
    margin_type: str  # isolated / cross
    unrealized_pnl: float = 0.0
    liquidation_price: float = 0.0
    mode: str = MODE_MOCK


def to_side(direction: str) -> str:
    """LONG -> BUY, SHORT -> SELL."""
    return "BUY" if direction.upper() == "LONG" else "SELL"


def opposite_side(side: str) -> str:
    return "SELL" if side.upper() == "BUY" else "BUY"


class ExchangeAdapter:
    """Common interface for all exchange adapters."""

    name = "base"
    mode = MODE_MOCK

    async def connect(self) -> bool:
        """Verify credentials / connectivity. Returns True on success."""
        raise NotImplementedError

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        raise NotImplementedError

    async def get_positions(self) -> list[PositionInfo]:
        raise NotImplementedError

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[OrderResult]:
        """
        Read-only: open (working) orders, optionally for one symbol. Used by the
        reconciliation (21B) and recovery (21C) engines to detect TP/SL drift.
        Default returns [] for adapters that do not implement it yet.
        """
        return []

    async def cancel_all_orders(self, symbol: str) -> int:
        """
        Cancel all working orders for a symbol — used by emergency close to clear
        stale TP/SL before a reduce-only market close. This NEVER opens or
        increases a position. Default no-op for adapters not implementing it.
        """
        return 0

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        raise NotImplementedError

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        raise NotImplementedError

    async def open_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """Place an order.

        ``client_order_id`` is an idempotency key echoed back by the exchange.
        Supplying a stable id lets a caller resolve an ambiguous timeout (did
        the order land?) via :meth:`get_order_by_client_id` instead of blindly
        retrying and risking a duplicate fill.
        """
        raise NotImplementedError

    async def close_order(self, *, symbol: str, side: str, qty: float) -> OrderResult:
        """Reduce-only close in the opposite direction of `side`."""
        raise NotImplementedError

    async def set_tp_sl(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        trailing_pct: Optional[float] = None,
    ) -> list[OrderResult]:
        raise NotImplementedError

    async def get_order_status(self, *, symbol: str, order_id: str) -> OrderResult:
        raise NotImplementedError

    async def get_order_by_client_id(
        self, *, symbol: str, client_order_id: str
    ) -> Optional[OrderResult]:
        """Look up an order by its client_order_id (idempotency key).

        Returns the order if the exchange has it, or ``None`` if no such order
        exists. Used to resolve ambiguous timeouts. Default ``None`` for
        adapters that do not implement it yet.
        """
        return None

    async def close(self) -> None:
        """Release any network resources."""
        return None
