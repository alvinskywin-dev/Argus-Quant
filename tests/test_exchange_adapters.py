"""
Sprint 20F — unit tests for the exchange adapter layer (no network).

The most safety-critical property is exercised here: resolve_adapter must NEVER
return a real (LIVE) adapter unless the live-trading gate is fully open.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac

import pytest

from app.config import settings
from app.exchange_adapters import live_gate_open, resolve_adapter
from app.exchange_adapters.base import MODE_LIVE, MODE_MOCK, AdapterError
from app.exchange_adapters.binance import BinanceFuturesAdapter, sign_query
from app.exchange_adapters.bitget import BitgetAdapter, sign_bitget
from app.exchange_adapters.bybit import BybitAdapter, sign_bybit
from app.exchange_adapters.mock import MockExchangeAdapter
from app.exchange_adapters.okx import OKXAdapter, sign_okx, to_inst_id


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
        "symbol": "LTCBTC",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": 1,
        "price": "0.1",
        "recvWindow": 5000,
        "timestamp": 1499827319559,
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


# ── live-safety audit V2: every live adapter guards identically ────
# All four guards must delegate to the single canonical live_gate_open() so
# they can never drift from the gate definition. Exercised across the full
# flag matrix; only (live=True, mock=False) may pass.

_GUARDS = [
    BinanceFuturesAdapter._guard,
    OKXAdapter._guard,
    BybitAdapter._guard,
    BitgetAdapter._guard,
]

_CLOSED_STATES = [
    (False, True),  # default
    (True, True),  # live on but mock also on -> closed
    (False, False),  # live off -> closed
]


@pytest.mark.parametrize("guard", _GUARDS, ids=["binance", "okx", "bybit", "bitget"])
@pytest.mark.parametrize("live_on,mock_on", _CLOSED_STATES)
def test_all_adapter_guards_refuse_when_gate_closed(gate, guard, live_on, mock_on):
    gate.live_trading_enabled = live_on
    gate.mock_exchange_mode = mock_on
    assert not live_gate_open()
    with pytest.raises(AdapterError):
        guard()


@pytest.mark.parametrize("guard", _GUARDS, ids=["binance", "okx", "bybit", "bitget"])
def test_all_adapter_guards_pass_only_when_gate_open(gate, guard):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    assert live_gate_open()
    guard()  # must not raise


@pytest.mark.parametrize("guard", _GUARDS, ids=["binance", "okx", "bybit", "bitget"])
def test_guards_track_canonical_gate_definition(gate, guard, monkeypatch):
    """A guard must follow live_gate_open(), not a divergent inline copy.

    If the canonical gate is forced closed, the guard must refuse even while
    the raw flags read 'open' — proving delegation to the single source."""
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    # Each adapter imports live_gate_open into its own module namespace, so
    # patch it there (guard.__module__ == 'app.exchange_adapters.<exchange>').
    monkeypatch.setattr(f"{guard.__module__}.live_gate_open", lambda: False)
    with pytest.raises(AdapterError):
        guard()


# ── mock adapter behaviour ────────────────────────────────────────


def test_mock_adapter_open_close():
    async def run():
        a = MockExchangeAdapter("binance")
        assert await a.connect() is True
        o = await a.open_order(symbol="BTCUSDT", side="BUY", qty=0.1, price=50000)
        assert o.status == "FILLED" and o.mode == MODE_MOCK and o.filled_qty == 0.1
        c = await a.close_order(symbol="BTCUSDT", side="BUY", qty=0.1)
        assert c.reduce_only and c.side == "SELL"
        tpsl = await a.set_tp_sl(
            symbol="BTCUSDT",
            side="BUY",
            qty=0.1,
            take_profit=55000,
            stop_loss=48000,
            trailing_pct=1.0,
        )
        assert {r.type for r in tpsl} == {
            "TAKE_PROFIT_MARKET",
            "STOP_MARKET",
            "TRAILING_STOP_MARKET",
        }

    asyncio.run(run())


# ── Sprint 20G: OKX / Bybit / Bitget ──────────────────────────────

# Each exchange signs a different prehash string; these tests lock the exact
# concatenation order (the #1 source of silent auth failures) by recomputing
# the expected digest independently from the documented format.


def test_okx_signature_format_and_order():
    secret, ts = "topsecret", "2026-05-31T00:00:00.000Z"
    expected = base64.b64encode(
        hmac.new(secret.encode(), f"{ts}POST/api/v5/trade/order".encode(), hashlib.sha256).digest()
    ).decode()
    assert sign_okx(secret, ts, "post", "/api/v5/trade/order", "") == expected


def test_bybit_signature_format_and_order():
    secret, ts, key, rw = "topsecret", "1700000000000", "mykey", "5000"
    payload = '{"category":"linear"}'
    expected = hmac.new(
        secret.encode(), f"{ts}{key}{rw}{payload}".encode(), hashlib.sha256
    ).hexdigest()
    assert sign_bybit(secret, ts, key, rw, payload) == expected


def test_bitget_signature_format_and_order():
    secret, ts = "topsecret", "1700000000000"
    expected = base64.b64encode(
        hmac.new(
            secret.encode(), f"{ts}GET/api/v2/mix/account/accounts".encode(), hashlib.sha256
        ).digest()
    ).decode()
    assert sign_bitget(secret, ts, "get", "/api/v2/mix/account/accounts", "") == expected


def test_okx_inst_id_mapping():
    assert to_inst_id("BTCUSDT") == "BTC-USDT-SWAP"
    assert to_inst_id("ethusdc") == "ETH-USDC-SWAP"


# ── routing: resolve_adapter returns the right LIVE adapter ────────


def test_resolve_routes_to_each_live_adapter(gate):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    assert isinstance(
        resolve_adapter("okx", api_key="k", api_secret="s", passphrase="p"), OKXAdapter
    )
    assert isinstance(resolve_adapter("bybit", api_key="k", api_secret="s"), BybitAdapter)
    assert isinstance(
        resolve_adapter("bitget", api_key="k", api_secret="s", passphrase="p"), BitgetAdapter
    )


def test_resolve_20g_adapters_mock_when_gate_closed(gate):
    gate.live_trading_enabled = False
    gate.mock_exchange_mode = True
    for ex in ("okx", "bybit", "bitget"):
        a = resolve_adapter(ex, api_key="k", api_secret="s", passphrase="p")
        assert isinstance(a, MockExchangeAdapter) and a.mode == MODE_MOCK


def test_passphrase_threaded_to_okx_and_bitget(gate):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    assert resolve_adapter("okx", api_key="k", api_secret="s", passphrase="pp")._passphrase == "pp"
    assert (
        resolve_adapter("bitget", api_key="k", api_secret="s", passphrase="pp")._passphrase == "pp"
    )


# ── defense in depth: every 20G adapter guards the gate ────────────


def test_20g_guards_raise_when_gate_closed(gate):
    gate.live_trading_enabled = False
    gate.mock_exchange_mode = True
    for cls in (OKXAdapter, BybitAdapter, BitgetAdapter):
        with pytest.raises(AdapterError):
            cls._guard()


def test_20g_guards_pass_when_gate_open(gate):
    gate.live_trading_enabled = True
    gate.mock_exchange_mode = False
    OKXAdapter._guard()
    BybitAdapter._guard()
    BitgetAdapter._guard()


# ── auto-routing (Signal → connected exchange) ────────────────────


class _Acct:
    def __init__(self, exchange, status="CONNECTED"):
        self.exchange, self.status = exchange, status


def _patch_vault(monkeypatch, accounts):
    from app.execution.live_trading import service

    async def fake_list_accounts(db, user_id):
        return accounts

    monkeypatch.setattr(service.vault, "list_accounts", fake_list_accounts)
    return service


def test_route_prefers_connected_match(monkeypatch):
    service = _patch_vault(monkeypatch, [_Acct("binance"), _Acct("bybit")])
    assert asyncio.run(service.route_exchange(None, 1, preferred="bybit")) == "bybit"


def test_route_falls_back_to_first_connected(monkeypatch):
    service = _patch_vault(monkeypatch, [_Acct("okx"), _Acct("bybit")])
    # preferred not connected -> first connected
    assert asyncio.run(service.route_exchange(None, 1, preferred="binance")) == "okx"
    # no preference -> first connected
    assert asyncio.run(service.route_exchange(None, 1)) == "okx"


def test_route_ignores_non_connected_accounts(monkeypatch):
    service = _patch_vault(monkeypatch, [_Acct("okx", status="ERROR"), _Acct("bybit")])
    assert asyncio.run(service.connected_exchanges(None, 1)) == ["bybit"]


def test_route_raises_when_nothing_connected(monkeypatch):
    service = _patch_vault(monkeypatch, [])
    with pytest.raises(service.LiveTradingError) as ei:
        asyncio.run(service.route_exchange(None, 1))
    assert ei.value.status_code == 400
