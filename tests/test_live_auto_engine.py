"""
Per-user real-money auto-trading (_open_live).

A real order is placed only when ALL gates pass: global live gate open + the user
has a connected exchange key + open_position succeeds. Any unmet gate or failure
SKIPs (logged) — it must never silently fall back to a demo/paper fill.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.auto_engine import engine
from app.auto_engine import service as auto_service
from app.exchange_adapters import base as adapters_base  # noqa: F401  (ensure import ok)
from app.execution.live_trading import service as live


@pytest.fixture
def patched(monkeypatch):
    """Stub out DB logging + live service; capture calls."""
    calls = {"log": [], "open": []}

    async def fake_log(db, **kw):
        calls["log"].append(kw)

    monkeypatch.setattr(auto_service, "log_execution", fake_log)

    async def fake_connected(db, user_id):
        return calls.get("exchanges", ["binance"])

    monkeypatch.setattr(live, "connected_exchanges", fake_connected)

    async def fake_open(db, **kw):
        calls["open"].append(kw)
        return {"mode": "LIVE", "order_id": "abc123"}

    monkeypatch.setattr(live, "open_position", fake_open)
    return calls


def _sig():
    return SimpleNamespace(
        id=7,
        symbol="BTCUSDT",
        side="LONG",
        stop_loss=60000.0,
        tp1=64000.0,
        tp2=66000.0,
        tp3=70000.0,
        confidence=90.0,
    )


def _cfg():
    return SimpleNamespace(
        live_enabled=True, risk_per_trade_pct=2.0, order_type="MARKET", max_leverage=3
    )


def _dec():
    return SimpleNamespace(leverage=3, risk_pct=2.0)


def _set_gate(monkeypatch, is_open: bool):
    # _open_live imports live_gate_open from app.exchange_adapters at call time,
    # so patching the module attribute is picked up.
    import app.exchange_adapters as xa

    monkeypatch.setattr(xa, "live_gate_open", lambda: is_open)


@pytest.mark.asyncio
async def test_skip_when_gate_closed(patched, monkeypatch):
    _set_gate(monkeypatch, False)
    ok = await engine._open_live(None, 32, _cfg(), _sig(), _dec(), 17)
    assert ok is False
    assert not patched["open"]  # no real order attempted
    assert patched["log"][-1]["reason"] == "live_gate_closed"


@pytest.mark.asyncio
async def test_skip_when_no_connected_exchange(patched, monkeypatch):
    _set_gate(monkeypatch, True)
    patched["exchanges"] = []
    ok = await engine._open_live(None, 32, _cfg(), _sig(), _dec(), 17)
    assert ok is False
    assert not patched["open"]
    assert patched["log"][-1]["reason"] == "live_no_exchange"


@pytest.mark.asyncio
async def test_places_real_order_when_all_gates_pass(patched, monkeypatch):
    _set_gate(monkeypatch, True)
    ok = await engine._open_live(None, 32, _cfg(), _sig(), _dec(), 17)
    assert ok is True
    assert len(patched["open"]) == 1
    args = patched["open"][0]
    assert args["exchange"] == "auto"
    assert args["symbol"] == "BTCUSDT" and args["side"] == "LONG"
    assert args["stop_loss"] == 60000.0 and args["take_profit"] == 64000.0
    assert args["leverage"] == 3 and args["risk_pct"] == 2.0
    assert args["order_type"] == "MARKET"
    assert patched["log"][-1]["action"] == "OPEN"


@pytest.mark.asyncio
async def test_skip_when_open_position_fails(patched, monkeypatch):
    _set_gate(monkeypatch, True)

    async def boom(db, **kw):
        raise live.LiveTradingError(400, "insufficient balance")

    monkeypatch.setattr(live, "open_position", boom)
    ok = await engine._open_live(None, 32, _cfg(), _sig(), _dec(), 17)
    assert ok is False
    assert patched["log"][-1]["reason"] == "live_failed"
