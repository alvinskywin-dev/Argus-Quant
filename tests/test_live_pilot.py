"""Live Pilot — gating + safety-check logic (no network/DB).

Pins that the pilot never opens without: the flag, the confirmation phrase,
stop-loss + take-profit, the designated user, the hard limits, and a clear
safety layer. DB-touching checks run against monkeypatched helpers.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.live_trading import pilot
from app.live_trading.pilot import PILOT_CONFIRM_PHRASE, PilotPreflight
from app.live_trading.service import LiveTradingError


@pytest.fixture
def pcfg():
    keys = (
        "live_pilot_enabled",
        "live_pilot_user_id",
        "live_pilot_max_notional",
        "live_pilot_max_positions",
        "live_pilot_max_leverage",
        "live_pilot_allowed_symbols",
        "live_pilot_require_confirmation",
        "auto_trading_enabled",
    )
    saved = {k: getattr(settings, k) for k in keys}
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _enable(cfg):
    cfg.live_pilot_enabled = True
    cfg.live_pilot_user_id = 1
    cfg.live_pilot_max_notional = 50.0
    cfg.live_pilot_max_positions = 2
    cfg.live_pilot_max_leverage = 3
    cfg.live_pilot_allowed_symbols = "BTCUSDT,ETHUSDT"
    cfg.live_pilot_require_confirmation = True
    cfg.auto_trading_enabled = False


def _checks(symbol="BTCUSDT", notional=20.0, leverage=2):
    return {
        c.name: c
        for c in pilot.validate_pilot_request(
            symbol=symbol, notional_usdt=notional, leverage=leverage
        )
    }


# ── static-limit checks ───────────────────────────────────────────


def test_disabled_flag_fails_check(pcfg):
    _enable(pcfg)
    pcfg.live_pilot_enabled = False
    assert _checks()["pilot_enabled"].ok is False


def test_symbol_not_allowed(pcfg):
    _enable(pcfg)
    assert _checks(symbol="DOGEUSDT")["symbol_allowed"].ok is False
    assert _checks(symbol="BTCUSDT")["symbol_allowed"].ok is True


def test_leverage_cap(pcfg):
    _enable(pcfg)
    assert _checks(leverage=5)["leverage_within_cap"].ok is False
    assert _checks(leverage=3)["leverage_within_cap"].ok is True


def test_notional_cap(pcfg):
    _enable(pcfg)
    assert _checks(notional=100.0)["notional_within_cap"].ok is False
    assert _checks(notional=50.0)["notional_within_cap"].ok is True


def test_auto_trading_must_be_off(pcfg):
    _enable(pcfg)
    pcfg.auto_trading_enabled = True
    assert _checks()["auto_trading_off"].ok is False


# ── pilot_open guards (no DB needed) ──────────────────────────────


def test_open_refused_when_disabled(pcfg):
    _enable(pcfg)
    pcfg.live_pilot_enabled = False
    with pytest.raises(LiveTradingError) as ei:
        asyncio.run(
            pilot.pilot_open(
                None,
                user_id=1,
                symbol="BTCUSDT",
                side="LONG",
                notional_usdt=20,
                leverage=2,
                stop_loss=1.0,
                take_profit=2.0,
                confirm=PILOT_CONFIRM_PHRASE,
            )
        )
    assert ei.value.status_code == 403


def test_open_refused_wrong_confirmation(pcfg):
    _enable(pcfg)
    with pytest.raises(LiveTradingError) as ei:
        asyncio.run(
            pilot.pilot_open(
                None,
                user_id=1,
                symbol="BTCUSDT",
                side="LONG",
                notional_usdt=20,
                leverage=2,
                stop_loss=1.0,
                take_profit=2.0,
                confirm="nope",
            )
        )
    assert ei.value.status_code == 400


def test_open_refused_without_stop_and_tp(pcfg):
    _enable(pcfg)
    with pytest.raises(LiveTradingError) as ei:
        asyncio.run(
            pilot.pilot_open(
                None,
                user_id=1,
                symbol="BTCUSDT",
                side="LONG",
                notional_usdt=20,
                leverage=2,
                stop_loss=None,
                take_profit=2.0,
                confirm=PILOT_CONFIRM_PHRASE,
            )
        )
    assert ei.value.status_code == 400


# ── preflight (monkeypatched DB/safety) ───────────────────────────


def _patch_preflight_env(monkeypatch, *, blocked=None, open_count=0, dup=False):
    async def _blocked(db, uid):
        return blocked

    async def _count(db, uid):
        return open_count

    async def _has(db, uid, sym):
        return dup

    async def _balance(db, *, user_id, exchange):
        return {"available": 1000, "asset": "USDT", "mode": "MOCK"}

    monkeypatch.setattr(pilot.safety, "trading_blocked", _blocked)
    monkeypatch.setattr(pilot, "_open_position_count", _count)
    monkeypatch.setattr(pilot, "_has_open_symbol", _has)
    monkeypatch.setattr(pilot.service, "get_balance", _balance)


def test_preflight_all_clear(pcfg, monkeypatch):
    _enable(pcfg)
    _patch_preflight_env(monkeypatch)
    pf = asyncio.run(
        pilot.pilot_preflight(
            None,
            user_id=1,
            symbol="BTCUSDT",
            notional_usdt=20,
            leverage=2,
            has_stop_loss=True,
            has_take_profit=True,
        )
    )
    assert isinstance(pf, PilotPreflight)
    assert pf.ok is True


def test_preflight_blocked_by_safety(pcfg, monkeypatch):
    _enable(pcfg)
    _patch_preflight_env(monkeypatch, blocked="global emergency stop active")
    pf = asyncio.run(
        pilot.pilot_preflight(
            None,
            user_id=1,
            symbol="BTCUSDT",
            notional_usdt=20,
            leverage=2,
            has_stop_loss=True,
            has_take_profit=True,
        )
    )
    assert pf.ok is False
    assert any(c.name == "safety_clear" and not c.ok for c in pf.checks)


def test_preflight_rejects_wrong_user(pcfg, monkeypatch):
    _enable(pcfg)
    _patch_preflight_env(monkeypatch)
    pf = asyncio.run(
        pilot.pilot_preflight(
            None,
            user_id=999,
            symbol="BTCUSDT",
            notional_usdt=20,
            leverage=2,
            has_stop_loss=True,
            has_take_profit=True,
        )
    )
    assert pf.ok is False
    assert any(c.name == "designated_pilot_user" and not c.ok for c in pf.checks)


def test_preflight_position_cap_and_dup(pcfg, monkeypatch):
    _enable(pcfg)
    _patch_preflight_env(monkeypatch, open_count=2, dup=True)
    pf = asyncio.run(
        pilot.pilot_preflight(
            None,
            user_id=1,
            symbol="BTCUSDT",
            notional_usdt=20,
            leverage=2,
            has_stop_loss=True,
            has_take_profit=True,
        )
    )
    assert pf.ok is False
    names = {c.name for c in pf.checks if not c.ok}
    assert "position_cap" in names and "no_existing_symbol_position" in names


def test_open_delegates_when_preflight_passes(pcfg, monkeypatch):
    _enable(pcfg)

    async def _pf(*a, **k):
        return PilotPreflight(ok=True, mode="MOCK")

    async def _open(db, **kw):
        return {"mode": "MOCK", "position_id": 1}

    monkeypatch.setattr(pilot, "pilot_preflight", _pf)
    monkeypatch.setattr(pilot.service, "open_position", _open)
    res = asyncio.run(
        pilot.pilot_open(
            None,
            user_id=1,
            symbol="BTCUSDT",
            side="LONG",
            notional_usdt=20,
            leverage=2,
            stop_loss=1.0,
            take_profit=2.0,
            confirm=PILOT_CONFIRM_PHRASE,
        )
    )
    assert res["pilot"] is True
    assert res["mode"] == "MOCK"
