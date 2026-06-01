"""
Sprint 21 — emergency-close safety invariants.

The full emergency_close_position flow is DB+adapter bound (see
tests/e2e_live_manual). Here we pin the two safety invariants that must never
regress:

  1. the action requires an exact confirmation phrase, and
  2. closing is ALWAYS reduce-only in the OPPOSITE direction — it can never
     open or flip a position.
"""
from __future__ import annotations

import pytest

from app.exchange_adapters.base import opposite_side, to_side
from app.exchange_adapters.mock import MockExchangeAdapter
from app.live_trading import service


def test_confirmation_phrase_is_exact():
    assert service.EMERGENCY_CONFIRM_PHRASE == "CLOSE UNSAFE POSITION"


def test_opposite_side_helper():
    assert opposite_side("BUY") == "SELL"
    assert opposite_side("SELL") == "BUY"
    assert to_side("LONG") == "BUY"
    assert to_side("SHORT") == "SELL"


@pytest.mark.asyncio
async def test_close_order_is_reduce_only_opposite_direction():
    adapter = MockExchangeAdapter("binance")
    # Closing a LONG (entry side BUY) must submit a reduce-only SELL.
    res = await adapter.close_order(symbol="BTCUSDT", side=to_side("LONG"), qty=1.0)
    assert res.side == "SELL"
    assert res.reduce_only is True
    # Closing a SHORT (entry side SELL) must submit a reduce-only BUY.
    res2 = await adapter.close_order(symbol="BTCUSDT", side=to_side("SHORT"), qty=1.0)
    assert res2.side == "BUY"
    assert res2.reduce_only is True


@pytest.mark.asyncio
async def test_cancel_all_orders_default_is_noop_safe():
    # Base/mock cancel_all_orders must never raise and never place orders.
    adapter = MockExchangeAdapter("binance")
    assert await adapter.cancel_all_orders("BTCUSDT") == 0
