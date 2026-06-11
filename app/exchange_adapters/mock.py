"""
Sprint 20F — Mock exchange adapter.

Simulates every operation in-process with NO network calls and NO real orders.
This is what every user gets unless the live-trading gate is explicitly open,
so the whole live-trading pipeline (orders, positions, TP/SL, audit) is fully
exercisable and safe by default.
"""

from __future__ import annotations

import itertools
from typing import Optional

from app.exchange_adapters.base import (
    MODE_MOCK,
    BalanceInfo,
    ExchangeAdapter,
    OrderResult,
    PositionInfo,
    opposite_side,
)

_order_seq = itertools.count(1)


def _mark(symbol: str, fallback: float) -> float:
    try:
        from app.market_data.ws_engine import latest_prices

        return float(latest_prices.get(symbol, fallback) or fallback)
    except Exception:
        return fallback


class MockExchangeAdapter(ExchangeAdapter):
    """Deterministic, offline simulation of an exchange."""

    mode = MODE_MOCK

    def __init__(self, exchange: str = "binance", balance: float = 10_000.0):
        self.name = f"{exchange}-mock"
        self.exchange = exchange
        self._balance = balance
        self._leverage: dict[str, int] = {}
        self._margin: dict[str, str] = {}
        # Idempotency registry: client_order_id -> OrderResult (mirrors a real
        # exchange rejecting/deduping a duplicate newClientOrderId).
        self._orders_by_cid: dict[str, OrderResult] = {}

    async def connect(self) -> bool:
        return True

    async def get_balance(self, asset: str = "USDT") -> BalanceInfo:
        return BalanceInfo(
            asset=asset, balance=self._balance, available=self._balance, mode=MODE_MOCK
        )

    async def get_positions(self) -> list[PositionInfo]:
        # Mock holds no server-side position state; positions are tracked in the DB.
        return []

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._leverage[symbol] = int(leverage)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self._margin[symbol] = margin_type.lower()

    async def open_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,  # idempotency key (accepted; passthrough/no-op)
    ) -> OrderResult:
        # Idempotent on client_order_id: a repeat returns the original order
        # instead of simulating a second fill.
        if client_order_id and client_order_id in self._orders_by_cid:
            return self._orders_by_cid[client_order_id]

        fill = float(price) if price else _mark(symbol, price or 0.0)
        oid = client_order_id or f"MOCK-{next(_order_seq)}"
        status = "FILLED" if order_type == "MARKET" else "NEW"
        result = OrderResult(
            order_id=oid,
            symbol=symbol,
            side=side,
            type=order_type,
            status=status,
            price=fill,
            qty=qty,
            filled_qty=qty if status == "FILLED" else 0.0,
            avg_price=fill if status == "FILLED" else 0.0,
            reduce_only=reduce_only,
            mode=MODE_MOCK,
            raw={"simulated": True, "client_order_id": client_order_id},
        )
        if client_order_id:
            self._orders_by_cid[client_order_id] = result
        return result

    async def get_order_by_client_id(
        self, *, symbol: str, client_order_id: str
    ) -> Optional[OrderResult]:
        return self._orders_by_cid.get(client_order_id)

    async def close_order(self, *, symbol: str, side: str, qty: float) -> OrderResult:
        return await self.open_order(
            symbol=symbol,
            side=opposite_side(side),
            qty=qty,
            order_type="MARKET",
            reduce_only=True,
        )

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
        out: list[OrderResult] = []
        close_side = opposite_side(side)
        if take_profit:
            out.append(
                OrderResult(
                    order_id=f"MOCK-{next(_order_seq)}",
                    symbol=symbol,
                    side=close_side,
                    type="TAKE_PROFIT_MARKET",
                    status="NEW",
                    price=take_profit,
                    qty=qty,
                    reduce_only=True,
                    mode=MODE_MOCK,
                )
            )
        if stop_loss:
            out.append(
                OrderResult(
                    order_id=f"MOCK-{next(_order_seq)}",
                    symbol=symbol,
                    side=close_side,
                    type="STOP_MARKET",
                    status="NEW",
                    price=stop_loss,
                    qty=qty,
                    reduce_only=True,
                    mode=MODE_MOCK,
                )
            )
        if trailing_pct:
            out.append(
                OrderResult(
                    order_id=f"MOCK-{next(_order_seq)}",
                    symbol=symbol,
                    side=close_side,
                    type="TRAILING_STOP_MARKET",
                    status="NEW",
                    price=0.0,
                    qty=qty,
                    reduce_only=True,
                    mode=MODE_MOCK,
                    raw={"callbackRate": trailing_pct},
                )
            )
        return out

    async def get_order_status(self, *, symbol: str, order_id: str) -> OrderResult:
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side="BUY",
            type="MARKET",
            status="FILLED",
            mode=MODE_MOCK,
        )
