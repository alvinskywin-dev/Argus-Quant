"""
Binance execution robustness — real fill reconciliation (#6) + server-time
sync (#7).

No network: the adapter's ``_request`` is stubbed per test, so only the unit
logic (fill merge, clock-offset computation, TTL) is exercised.
"""

from __future__ import annotations

import time

import pytest

from app.exchange_adapters.base import AdapterError, OrderResult
from app.exchange_adapters.binance import BinanceFuturesAdapter


def _adapter() -> BinanceFuturesAdapter:
    return BinanceFuturesAdapter("key", "secret", testnet=True)


# ── #6 real fill reconciliation ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_reconcile_fill_merges_real_avg_price():
    adapter = _adapter()

    async def fake_request(method, path, params=None, *, signed=True, _retry_on_drift=True):
        # get_order_status re-read returns the true fill.
        return {
            "orderId": "1",
            "status": "FILLED",
            "avgPrice": "50010.5",
            "executedQty": "0.01",
            "origQty": "0.01",
        }

    adapter._request = fake_request
    res = OrderResult(
        order_id="1", symbol="BTCUSDT", side="BUY", type="MARKET", status="NEW", avg_price=0.0
    )
    out = await adapter._reconcile_fill("BTCUSDT", res)
    assert out.avg_price == pytest.approx(50010.5)
    assert out.price == pytest.approx(50010.5)
    assert out.filled_qty == pytest.approx(0.01)
    assert out.status == "FILLED"


@pytest.mark.asyncio
async def test_reconcile_fill_keeps_original_on_lookup_error():
    adapter = _adapter()

    async def fake_request(*a, **k):
        raise AdapterError("network blip")

    adapter._request = fake_request
    res = OrderResult(
        order_id="1", symbol="BTCUSDT", side="BUY", type="MARKET", status="NEW", avg_price=0.0
    )
    out = await adapter._reconcile_fill("BTCUSDT", res)
    assert out.avg_price == 0.0  # unchanged, no crash


@pytest.mark.asyncio
async def test_reconcile_fill_noop_when_exchange_has_no_fill_price():
    adapter = _adapter()

    async def fake_request(method, path, params=None, *, signed=True, _retry_on_drift=True):
        return {"orderId": "1", "status": "NEW", "avgPrice": "0", "executedQty": "0"}

    adapter._request = fake_request
    res = OrderResult(
        order_id="1", symbol="BTCUSDT", side="BUY", type="MARKET", status="NEW", avg_price=0.0
    )
    out = await adapter._reconcile_fill("BTCUSDT", res)
    assert out.avg_price == 0.0


# ── #7 server-time sync ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_sync_time_computes_offset():
    adapter = _adapter()

    async def fake_request(method, path, params=None, *, signed=True, _retry_on_drift=True):
        assert path == "/fapi/v1/time"
        assert signed is False  # time endpoint is public/unsigned
        return {"serverTime": int(time.time() * 1000) + 8000}  # server ~8s ahead

    adapter._request = fake_request
    await adapter._sync_time(force=True)
    assert 6000 < adapter._time_offset_ms < 10000
    assert adapter._time_synced_at > 0


@pytest.mark.asyncio
async def test_sync_time_missing_servertime_keeps_offset():
    adapter = _adapter()
    adapter._time_offset_ms = 123

    async def fake_request(method, path, params=None, *, signed=True, _retry_on_drift=True):
        return {}  # no serverTime

    adapter._request = fake_request
    await adapter._sync_time(force=True)
    assert adapter._time_offset_ms == 123  # unchanged


@pytest.mark.asyncio
async def test_ensure_time_offset_respects_ttl():
    adapter = _adapter()
    calls: list = []

    async def fake_request(method, path, params=None, *, signed=True, _retry_on_drift=True):
        calls.append(path)
        return {"serverTime": int(time.time() * 1000)}

    adapter._request = fake_request

    # Fresh sync -> no network call.
    adapter._time_synced_at = time.time()
    await adapter._ensure_time_offset()
    assert calls == []

    # Stale (never synced) -> syncs once.
    adapter._time_synced_at = 0.0
    await adapter._ensure_time_offset()
    assert calls == ["/fapi/v1/time"]


@pytest.mark.asyncio
async def test_ensure_time_offset_swallows_errors():
    adapter = _adapter()

    async def fake_request(*a, **k):
        raise AdapterError("time endpoint down")

    adapter._request = fake_request
    adapter._time_synced_at = 0.0
    # Must not raise — falls back to the local clock.
    await adapter._ensure_time_offset()
