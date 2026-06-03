"""Multi-user Live Beta — membership lifecycle + beta_gate (no network/DB).

DB-touching calls run against monkeypatched lookup/exposure helpers and a tiny
fake session (no async-sqlite driver in the suite).
"""

from __future__ import annotations

import asyncio

import pytest

import app.live_beta.service as service
from app.config import settings
from app.live_beta.models import APPROVED, PENDING, LiveBetaMember


@pytest.fixture
def bcfg():
    keys = (
        "live_beta_enabled",
        "live_beta_max_users",
        "live_beta_require_admin_approval",
        "live_beta_invite_code",
        "live_beta_global_max_notional",
        "live_beta_default_user_max_notional",
        "live_beta_default_max_positions",
        "live_beta_per_symbol_max_notional",
        "live_beta_allowed_exchanges",
    )
    saved = {k: getattr(settings, k) for k in keys}
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _enable(cfg, *, approval=True, invite=""):
    cfg.live_beta_enabled = True
    cfg.live_beta_max_users = 10
    cfg.live_beta_require_admin_approval = approval
    cfg.live_beta_invite_code = invite
    cfg.live_beta_global_max_notional = 500.0
    cfg.live_beta_default_user_max_notional = 100.0
    cfg.live_beta_default_max_positions = 2
    cfg.live_beta_per_symbol_max_notional = 100.0
    cfg.live_beta_allowed_exchanges = "binance"


class _FakeDB:
    def __init__(self):
        self.added: list = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for o in self.added:
            if isinstance(o, LiveBetaMember) and getattr(o, "id", None) is None:
                o.id = 1


def _patch(monkeypatch, *, member=None, count=0):
    async def _get(db, uid):
        return member

    async def _count(db):
        return count

    monkeypatch.setattr(service, "get_member", _get)
    monkeypatch.setattr(service, "_member_count", _count)


# ── request_access ────────────────────────────────────────────────


def test_request_refused_when_disabled(bcfg):
    bcfg.live_beta_enabled = False
    with pytest.raises(service.LiveBetaError) as ei:
        asyncio.run(service.request_access(_FakeDB(), user_id=1, invite_code="", accept_risk=True))
    assert ei.value.status_code == 403


def test_request_requires_risk_acceptance(bcfg, monkeypatch):
    _enable(bcfg)
    _patch(monkeypatch)
    with pytest.raises(service.LiveBetaError) as ei:
        asyncio.run(service.request_access(_FakeDB(), user_id=1, invite_code="", accept_risk=False))
    assert ei.value.status_code == 400


def test_request_bad_invite_code(bcfg, monkeypatch):
    _enable(bcfg, invite="SECRET")
    _patch(monkeypatch)
    with pytest.raises(service.LiveBetaError) as ei:
        asyncio.run(
            service.request_access(_FakeDB(), user_id=1, invite_code="WRONG", accept_risk=True)
        )
    assert ei.value.status_code == 403


def test_request_full(bcfg, monkeypatch):
    _enable(bcfg)
    _patch(monkeypatch, count=10)
    with pytest.raises(service.LiveBetaError) as ei:
        asyncio.run(service.request_access(_FakeDB(), user_id=1, invite_code="", accept_risk=True))
    assert ei.value.status_code == 409


def test_request_creates_pending_when_approval_required(bcfg, monkeypatch):
    _enable(bcfg, approval=True)
    _patch(monkeypatch)
    m = asyncio.run(service.request_access(_FakeDB(), user_id=1, invite_code="", accept_risk=True))
    assert m.status == PENDING
    assert m.risk_agreement_accepted_at is not None


def test_request_auto_approves_when_not_required(bcfg, monkeypatch):
    _enable(bcfg, approval=False)
    _patch(monkeypatch)
    m = asyncio.run(service.request_access(_FakeDB(), user_id=1, invite_code="", accept_risk=True))
    assert m.status == APPROVED
    assert m.approved_at is not None


# ── beta_gate ─────────────────────────────────────────────────────


def _approved_member(**over):
    m = LiveBetaMember(
        user_id=1,
        status=APPROVED,
        max_notional=100.0,
        max_positions=2,
        allowed_exchanges="binance",
    )
    from datetime import datetime, timezone

    m.risk_agreement_accepted_at = datetime.now(timezone.utc)
    for k, v in over.items():
        setattr(m, k, v)
    return m


def _patch_exposure(
    monkeypatch, *, member, user_notional=0.0, positions=0, sym_notional=0.0, glob=0.0
):
    async def _get(db, uid):
        return member

    async def _un(db, uid):
        return user_notional

    async def _up(db, uid):
        return positions

    async def _sn(db, uid, sym):
        return sym_notional

    async def _gn(db):
        return glob

    monkeypatch.setattr(service, "get_member", _get)
    monkeypatch.setattr(service, "_user_open_notional", _un)
    monkeypatch.setattr(service, "_user_open_positions", _up)
    monkeypatch.setattr(service, "_symbol_open_notional", _sn)
    monkeypatch.setattr(service, "_global_open_notional", _gn)


def _gate(notional=20.0, exchange="binance", symbol="BTCUSDT"):
    return asyncio.run(
        service.beta_gate(
            _FakeDB(), user_id=1, exchange=exchange, symbol=symbol, notional_usdt=notional
        )
    )


def test_gate_noop_when_disabled(bcfg):
    bcfg.live_beta_enabled = False
    assert _gate() is None


def test_gate_blocks_non_member(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=None)
    assert "approved" in _gate()


def test_gate_blocks_unapproved(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=_approved_member(status=PENDING))
    assert _gate() is not None


def test_gate_blocks_disallowed_exchange(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=_approved_member())
    assert "exchange" in _gate(exchange="okx")


def test_gate_blocks_position_cap(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=_approved_member(), positions=2)
    assert "positions" in _gate()


def test_gate_blocks_user_notional(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=_approved_member(), user_notional=95.0)
    assert "per-user" in _gate(notional=20.0)


def test_gate_blocks_per_symbol(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=_approved_member(max_notional=1000.0), sym_notional=95.0)
    assert "per-symbol" in _gate(notional=20.0)


def test_gate_blocks_global_cap(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=_approved_member(max_notional=10000.0), glob=495.0)
    bcfg.live_beta_per_symbol_max_notional = 10000.0
    assert "global" in _gate(notional=20.0)


def test_gate_allows_within_limits(bcfg, monkeypatch):
    _enable(bcfg)
    _patch_exposure(monkeypatch, member=_approved_member())
    assert _gate(notional=20.0) is None
