"""
SignalTracker lifecycle integration (audit fixes #1, #5, #6, #8).

Exercises ``_check_one`` with a stub signal + stubbed repo so no DB/network is
needed: partial-TP/SL→break-even PnL, candle-wick detection, entry-fill gating,
and the redundant-write throttle.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.scanner.tracker as tracker_mod
from app.config import settings
from app.scanner.tracker import SignalTracker
from app.utils.helpers import utcnow


@pytest.fixture
def writes(monkeypatch):
    """Capture repo.update_signal calls; return the list of (id, fields)."""
    calls: list = []

    async def fake_update(signal_id, fields):
        calls.append((signal_id, dict(fields)))

    monkeypatch.setattr(tracker_mod.repo, "update_signal", fake_update)
    return calls


@pytest.fixture
def cfg():
    keys = [
        "lifecycle_pnl_enabled",
        "tp1_close_fraction",
        "tp2_close_fraction",
        "move_sl_to_breakeven_after_tp1",
        "tracker_use_candle_extremes",
        "entry_fill_required",
        "entry_fill_timeout_min",
        "tracker_min_pnl_delta_pct",
        "count_protected_sl_as_win",
    ]
    saved = {k: getattr(settings, k) for k in keys}
    settings.lifecycle_pnl_enabled = True
    settings.tp1_close_fraction = 0.30
    settings.tp2_close_fraction = 0.30
    settings.move_sl_to_breakeven_after_tp1 = True
    settings.tracker_use_candle_extremes = True
    settings.entry_fill_required = True
    settings.entry_fill_timeout_min = 90
    settings.tracker_min_pnl_delta_pct = 0.05
    settings.count_protected_sl_as_win = True
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _sig(**over):
    base = dict(
        id=1,
        symbol="TESTUSDT",
        side="LONG",
        entry_low=99.9,
        entry_high=100.1,
        tp1=102.0,
        tp2=104.0,
        tp3=107.0,
        stop_loss=98.0,
        status="OPEN",
        pnl_pct=0.0,
        max_favorable_pct=0.0,
        max_adverse_pct=0.0,
        diagnostics=json.dumps({"entry_fill": {"filled": True}}),
        telegram_message_id=None,
        created_at=utcnow(),
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_tp1_then_sl_books_partial(cfg, writes):
    # One candle wicks up to TP1 (high) then back through the stop (low).
    t = SignalTracker()
    sig = _sig()
    await t._check_one(sig, price=98.0, extreme=(102.5, 97.5))

    # Protected win: recorded status is the TP reached (not "SL") so no
    # raw-status consumer counts it as a stop. PnL is the booked partial, not −2%.
    final = writes[-1][1]
    assert final["status"] == "TP1"
    assert final["pnl_pct"] == pytest.approx(0.6, abs=0.01)
    # Lifecycle diagnostics preserve the true SL exit.
    diag = json.loads(final["diagnostics"])
    assert diag["tp_history"]["max_tp_hit"] == 1
    assert diag["tp_history"]["final_exit_event"] == "SL"


@pytest.mark.asyncio
async def test_protected_sl_as_win_can_be_disabled(cfg, writes):
    # With the flag off, the terminal status reverts to the raw "SL".
    cfg.count_protected_sl_as_win = False
    t = SignalTracker()
    sig = _sig()
    await t._check_one(sig, price=98.0, extreme=(102.5, 97.5))
    final = writes[-1][1]
    assert final["status"] == "SL"
    assert final["pnl_pct"] == pytest.approx(0.6, abs=0.01)


@pytest.mark.asyncio
async def test_clean_stop_is_full_loss(cfg, writes):
    t = SignalTracker()
    sig = _sig()
    await t._check_one(sig, price=97.5, extreme=(100.2, 97.5))  # never tags TP1
    final = writes[-1][1]
    assert final["status"] == "SL"
    assert final["pnl_pct"] == pytest.approx(-2.0, abs=0.01)


@pytest.mark.asyncio
async def test_entry_fill_required_blocks_until_band_touched(cfg, writes):
    t = SignalTracker()
    sig = _sig(diagnostics=None)  # not yet filled
    # Price/candle never reaches the entry band → no status change at all.
    await t._check_one(sig, price=105.0, extreme=(105.5, 104.5))
    assert all(f.get("status") not in {"TP1", "SL"} for _, f in writes)
    assert sig.status == "OPEN"


@pytest.mark.asyncio
async def test_unfilled_signal_expires_after_timeout(cfg, writes):
    from datetime import timedelta

    t = SignalTracker()
    old = utcnow() - timedelta(minutes=200)
    sig = _sig(diagnostics=None, created_at=old)
    await t._check_one(sig, price=105.0, extreme=(105.5, 104.5))
    assert writes[-1][1]["status"] == "EXPIRED"


@pytest.mark.asyncio
async def test_redundant_write_is_skipped(cfg, writes):
    t = SignalTracker()
    # Already-marked pnl essentially unchanged at current price → no DB write.
    sig = _sig(pnl_pct=0.0, max_favorable_pct=0.0, max_adverse_pct=0.0)
    await t._check_one(sig, price=100.0, extreme=(100.0, 100.0))
    assert writes == []
