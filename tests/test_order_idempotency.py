"""
Order idempotency + ambiguous-timeout resolution (live-safety #1 + #2).

A dropped connection on order placement is *ambiguous*: the order may already
have landed on the exchange. These tests pin the two safety properties:

  1. Orders carry a client_order_id and a repeat with the same id does NOT
     create a second fill (no duplicate).
  2. When placement raises AdapterTimeoutError, the service resolves the real
     state by client id — adopting the order if it landed, and only reporting
     failure when the exchange genuinely has no such order.
"""

from __future__ import annotations

import pytest

from app.exchange_adapters.base import (
    AdapterError,
    AdapterTimeoutError,
    OrderResult,
)
from app.exchange_adapters.mock import MockExchangeAdapter
from app.execution.live_trading import service


# ── 1. client_order_id idempotency on the mock adapter ────────────────────────
@pytest.mark.asyncio
async def test_mock_open_order_is_idempotent_on_client_id():
    adapter = MockExchangeAdapter()
    cid = "ax-test-001"
    o1 = await adapter.open_order(symbol="BTCUSDT", side="BUY", qty=0.01, client_order_id=cid)
    o2 = await adapter.open_order(symbol="BTCUSDT", side="BUY", qty=0.01, client_order_id=cid)
    assert o1.order_id == o2.order_id == cid
    # The registry holds exactly one order for that id.
    found = await adapter.get_order_by_client_id(symbol="BTCUSDT", client_order_id=cid)
    assert found is not None and found.order_id == cid


@pytest.mark.asyncio
async def test_mock_open_order_without_client_id_generates_unique():
    adapter = MockExchangeAdapter()
    o1 = await adapter.open_order(symbol="BTCUSDT", side="BUY", qty=0.01)
    o2 = await adapter.open_order(symbol="BTCUSDT", side="BUY", qty=0.01)
    assert o1.order_id != o2.order_id


# ── client_order_id builder ───────────────────────────────────────────────────
def test_build_client_order_id_generates_and_sanitises():
    gen = service._build_client_order_id(None)
    assert 1 <= len(gen) <= 36 and gen.startswith("ax")

    # Illegal characters are stripped; result stays within 36 chars.
    cleaned = service._build_client_order_id("sig#42@BTC USDT!*")
    assert len(cleaned) <= 36
    assert all(c in service._CID_ALLOWED for c in cleaned)

    # Empty-after-sanitise falls back to a generated id.
    assert service._build_client_order_id("***").startswith("ax")


# ── 2. ambiguous timeout resolution ───────────────────────────────────────────
class _TimeoutThenLandedAdapter:
    """Raises a timeout on open_order, but the order actually landed (queryable)."""

    def __init__(self):
        self.open_calls = 0

    async def open_order(self, **kw):
        self.open_calls += 1
        raise AdapterTimeoutError("connection dropped after send")

    async def get_order_by_client_id(self, *, symbol, client_order_id):
        return OrderResult(
            order_id="EX-999",
            symbol=symbol,
            side="BUY",
            type="MARKET",
            status="FILLED",
            filled_qty=0.01,
            avg_price=50000.0,
        )


class _TimeoutNotLandedAdapter:
    """Raises a timeout, and the order genuinely never reached the exchange."""

    async def open_order(self, **kw):
        raise AdapterTimeoutError("connection dropped before send")

    async def get_order_by_client_id(self, *, symbol, client_order_id):
        return None


@pytest.mark.asyncio
async def test_timeout_resolves_to_landed_order_no_duplicate():
    adapter = _TimeoutThenLandedAdapter()
    order = await service._open_entry_idempotent(
        adapter,
        symbol="BTCUSDT",
        side="BUY",
        qty=0.01,
        order_type="MARKET",
        price=None,
        client_order_id="ax-cid-1",
    )
    # The landed order is adopted; the entry was attempted exactly once.
    assert order.order_id == "EX-999"
    assert order.status == "FILLED"
    assert adapter.open_calls == 1


@pytest.mark.asyncio
async def test_timeout_not_landed_raises_clean_adaptererror():
    adapter = _TimeoutNotLandedAdapter()
    with pytest.raises(AdapterError) as ei:
        await service._open_entry_idempotent(
            adapter,
            symbol="BTCUSDT",
            side="BUY",
            qty=0.01,
            order_type="MARKET",
            price=None,
            client_order_id="ax-cid-2",
        )
    # Normalised to a non-timeout AdapterError so the caller records a clean failure.
    assert not isinstance(ei.value, AdapterTimeoutError)
    assert "ax-cid-2" in str(ei.value)


@pytest.mark.asyncio
async def test_resolve_after_timeout_retries_until_found():
    class _SlowAppear:
        def __init__(self):
            self.calls = 0

        async def get_order_by_client_id(self, *, symbol, client_order_id):
            self.calls += 1
            if self.calls < 2:
                return None
            return OrderResult(
                order_id="EX-1", symbol=symbol, side="BUY", type="MARKET", status="FILLED"
            )

    adapter = _SlowAppear()
    res = await service._resolve_after_timeout(adapter, "BTCUSDT", "cid", attempts=3, delay=0.0)
    assert res is not None and adapter.calls == 2
