"""
Sprint 20F — unit tests for the exchange adapter layer (no network).

The most safety-critical property is exercised here: resolve_adapter must NEVER
return a real (LIVE) adapter unless the live-trading gate is fully open.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.exchange_adapters import live_gate_open, resolve_adapter
from app.exchange_adapters.base import AdapterError, MODE_LIVE, MODE_MOCK
from app.exchange_adapters.binance import BinanceFuturesAdapter, sign_query
from app.exchange_adapters.mock import MockExchangeAdapter


@pytest.fixture
def gate():
    """Save/restore the two gate flags around a test."""
    orig = (settings.live_trading_enabled, settings.mock_exchange_mode)
    yield settings
    settings.live_trading_enabled, settings.mock_exchange_mode = orig


# ── Binance HMAC signature (documented test vector) ───────────────

def test_binance_signature_matches_documented_vector():
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    params = {
        "symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
        "quantity": 1, "price": "0.1", "recvWindow": 5000, "timestamp": 1499827319559,
    }
    expected = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"
    assert sign_query(secret, params) == expected


# ── the gate ──────────────────────────────────────────────────────

def test_resolve_returns_mock_by_default(gate):
    gate.live_trading_enabled = False
    gate.mock_exchange_mode = True
    a = resolve_adapter("binance", api_key="k", api_secret="s")
    assert isinstance(a, MockExchangeAdapter) and a.mode == MODE_MOCK
    assert not live_gate_open()


def test_resolve_mock_when_live_on_but_mock_also_on(gate):
    # Both must align; mock mode wins (safe).
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = True
    assert not live_gate_open()
    assert isinstance(resolve_adapter("binance", api_key="k", api_secret="s"), MockExchangeAdapter)


def test_resolve_mock_when_live_off_even_if_mock_off(gate):
    gate.live_trading_enabled = False
    gate.mock_exchange_mode = False
    assert not live_gate_open()
    assert isinstance(resolve_adapter("binance", api_key="k", api_secret="s"), MockExchangeAdapter)


def test_resolve_live_only_when_gate_fully_open(gate):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    assert live_gate_open()
    a = resolve_adapter("binance", api_key="k", api_secret="s")
    assert isinstance(a, BinanceFuturesAdapter) and a.mode == MODE_LIVE


def test_resolve_mock_when_no_credentials(gate):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    # Gate open but no creds -> still mock (cannot sign).
    assert isinstance(resolve_adapter("binance"), MockExchangeAdapter)


def test_unknown_exchange_falls_back_to_mock(gate):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    assert isinstance(resolve_adapter("ftx", api_key="k", api_secret="s"), MockExchangeAdapter)


# ── defense in depth: real adapter refuses when gate closed ───────

def test_binance_guard_raises_when_gate_closed(gate):
    gate.live_trading_enabled = False
    gate.mock_exchange_mode = True
    with pytest.raises(AdapterError):
        BinanceFuturesAdapter._guard()


def test_binance_guard_passes_when_gate_open(gate):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    BinanceFuturesAdapter._guard()  # should not raise


# ── mock adapter behaviour ────────────────────────────────────────

def test_mock_adapter_open_close():
    async def run():
        a = MockExchangeAdapter("binance")
        assert await a.connect() is True
        o = await a.open_order(symbol="BTCUSDT", side="BUY", qty=0.1, price=50000)
        assert o.status == "FILLED" and o.mode == MODE_MOCK and o.filled_qty == 0.1
        c = await a.close_order(symbol="BTCUSDT", side="BUY", qty=0.1)
        assert c.reduce_only and c.side == "SELL"
        tpsl = await a.set_tp_sl(symbol="BTCUSDT", side="BUY", qty=0.1,
                                 take_profit=55000, stop_loss=48000, trailing_pct=1.0)
        assert {r.type for r in tpsl} == {"TAKE_PROFIT_MARKET", "STOP_MARKET", "TRAILING_STOP_MARKET"}
    asyncio.run(run())
