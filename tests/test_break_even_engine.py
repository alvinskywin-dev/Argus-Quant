"""Sprint 22D — Break-Even + Partial TP Engine (planner; never widens SL)."""

from __future__ import annotations

import pytest

from app.config import settings
from app.live_trading.break_even import (
    BreakEvenIntent,
    PositionProtectionState,
    apply_intents,
    diagnostics,
    plan_tp1_actions,
    update_trailing_stop,
)


@pytest.fixture
def engine():
    keys = [
        "break_even_engine_enabled",
        "partial_tp_percent",
        "move_sl_to_entry_on_tp1",
        "trailing_stop_enabled",
        "trailing_stop_distance_percent",
    ]
    saved = {k: getattr(settings, k) for k in keys}
    settings.break_even_engine_enabled = True
    settings.partial_tp_percent = 40.0
    settings.move_sl_to_entry_on_tp1 = True
    settings.trailing_stop_enabled = True
    settings.trailing_stop_distance_percent = 1.5
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _plan_long(state=None, current_stop=95.0):
    return plan_tp1_actions(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=100.0,
        open_quantity=2.0,
        current_stop=current_stop,
        tp1_price=110.0,
        state=state or PositionProtectionState(),
    )


# ── disabled ─────────────────────────────────────────────────────────────────
def test_disabled_produces_nothing(engine):
    engine.break_even_engine_enabled = False
    assert _plan_long() == []


# ── partial close ────────────────────────────────────────────────────────────
def test_partial_close_quantity(engine):
    intents = _plan_long()
    pc = [i for i in intents if i.action == "PARTIAL_CLOSE"]
    assert len(pc) == 1
    assert pc[0].quantity == 0.8  # 40% of 2.0
    assert pc[0].reduce_only is True


def test_partial_close_percent_respected(engine):
    engine.partial_tp_percent = 50.0
    pc = [i for i in _plan_long() if i.action == "PARTIAL_CLOSE"][0]
    assert pc.quantity == 1.0  # 50% of 2.0


# ── break-even SL move ───────────────────────────────────────────────────────
def test_move_sl_to_entry(engine):
    move = [i for i in _plan_long() if i.action == "MOVE_SL"]
    assert len(move) == 1
    assert move[0].new_stop_price == 100.0
    assert move[0].reduce_only is True


def test_sl_never_widened_long(engine):
    # current stop already at entry (100) -> moving to 100 is not tighter -> skip
    intents = _plan_long(current_stop=100.0)
    assert not [i for i in intents if i.action == "MOVE_SL"]


def test_sl_move_disabled(engine):
    engine.move_sl_to_entry_on_tp1 = False
    assert not [i for i in _plan_long() if i.action == "MOVE_SL"]


# ── trailing ─────────────────────────────────────────────────────────────────
def test_arm_trailing(engine):
    arm = [i for i in _plan_long() if i.action == "ARM_TRAILING"]
    assert len(arm) == 1
    # 1.5% behind tp1 110 -> 108.35
    assert abs(arm[0].new_stop_price - 108.35) < 1e-6
    assert arm[0].trailing_distance_percent == 1.5


def test_trailing_disabled(engine):
    engine.trailing_stop_enabled = False
    assert not [i for i in _plan_long() if i.action == "ARM_TRAILING"]


def test_update_trailing_ratchets_up(engine):
    state = PositionProtectionState(trailing_active=True, trailing_stop_price=108.35)
    intent = update_trailing_stop(symbol="BTCUSDT", side="LONG", last_price=115.0, state=state)
    assert intent is not None
    assert intent.new_stop_price > 108.35  # ratcheted up


def test_update_trailing_no_widen(engine):
    state = PositionProtectionState(trailing_active=True, trailing_stop_price=113.0)
    # price dropped; candidate would be lower than current -> no update
    intent = update_trailing_stop(symbol="BTCUSDT", side="LONG", last_price=110.0, state=state)
    assert intent is None


def test_update_trailing_inactive(engine):
    state = PositionProtectionState(trailing_active=False)
    assert update_trailing_stop(symbol="X", side="LONG", last_price=120, state=state) is None


# ── short side ───────────────────────────────────────────────────────────────
def test_short_break_even_and_trailing(engine):
    intents = plan_tp1_actions(
        symbol="ETHUSDT",
        side="SHORT",
        entry_price=100.0,
        open_quantity=4.0,
        current_stop=105.0,
        tp1_price=90.0,
        state=PositionProtectionState(),
    )
    move = [i for i in intents if i.action == "MOVE_SL"][0]
    assert move.new_stop_price == 100.0  # entry
    arm = [i for i in intents if i.action == "ARM_TRAILING"][0]
    # 1.5% above tp1 90 -> 91.35 (tighter than entry for short)
    assert abs(arm.new_stop_price - 91.35) < 1e-6


def test_short_sl_never_widened(engine):
    # short stop already at entry -> not tighter -> skip
    intents = plan_tp1_actions(
        symbol="ETHUSDT",
        side="SHORT",
        entry_price=100.0,
        open_quantity=4.0,
        current_stop=100.0,
        tp1_price=90.0,
        state=PositionProtectionState(),
    )
    assert not [i for i in intents if i.action == "MOVE_SL"]


# ── idempotency + state ──────────────────────────────────────────────────────
def test_idempotent_after_apply(engine):
    state = PositionProtectionState()
    first = _plan_long(state=state)
    apply_intents(state, first)
    second = _plan_long(state=state)
    assert second == []  # nothing re-emitted


def test_apply_sets_flags(engine):
    state = PositionProtectionState()
    apply_intents(state, _plan_long(state=state))
    assert state.partial_tp_done is True
    assert state.break_even_done is True
    assert state.trailing_active is True


def test_diagnostics_shape(engine):
    state = PositionProtectionState()
    apply_intents(state, _plan_long(state=state))
    d = diagnostics(state).to_dict()
    assert d["partial_tp_executed"] is True
    assert d["break_even_activated"] is True
    assert d["trailing_stop_active"] is True


def test_zero_quantity_no_partial(engine):
    intents = plan_tp1_actions(
        symbol="X",
        side="LONG",
        entry_price=100,
        open_quantity=0,
        current_stop=95,
        tp1_price=110,
        state=PositionProtectionState(),
    )
    assert intents == []


def test_all_intents_reduce_only(engine):
    for i in _plan_long():
        assert i.reduce_only is True


def test_intent_to_dict():
    i = BreakEvenIntent(action="MOVE_SL", symbol="X", side="LONG", new_stop_price=100.0)
    d = i.to_dict()
    assert d["action"] == "MOVE_SL"
    assert d["reduce_only"] is True
