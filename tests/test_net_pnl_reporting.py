"""
Net-of-fees PnL reporting (#8).

Reported performance PnL is net of an estimated round-trip taker fee so it
reflects real trading cost instead of gross price move. These tests pin the
conversion and the on/off + custom-rate behaviour.
"""

from __future__ import annotations

import pytest

from app.accounting.pnl import net_pnl_pct, signal_net_pnl
from app.config import settings


@pytest.fixture
def fee_cfg():
    """Save/restore the fee-reporting settings around a test."""
    orig = (settings.report_fees_enabled, settings.report_roundtrip_fee_bps)
    yield
    settings.report_fees_enabled, settings.report_roundtrip_fee_bps = orig


def test_net_pnl_subtracts_roundtrip_fee(fee_cfg):
    settings.report_fees_enabled = True
    settings.report_roundtrip_fee_bps = 8.0  # 0.08 percentage points
    # +1.00% gross becomes +0.92% net.
    assert net_pnl_pct(1.00) == pytest.approx(0.92)
    # A marginal +0.05% gross is actually a net loss after fees.
    assert net_pnl_pct(0.05) == pytest.approx(-0.03)


def test_net_pnl_disabled_returns_gross(fee_cfg):
    settings.report_fees_enabled = False
    assert net_pnl_pct(1.00) == pytest.approx(1.00)


def test_net_pnl_custom_fee_bps(fee_cfg):
    settings.report_fees_enabled = True
    # Explicit fee_bps overrides the configured default.
    assert net_pnl_pct(1.00, fee_bps=20.0) == pytest.approx(0.80)


def test_net_pnl_handles_none():
    assert net_pnl_pct(None) == 0.0


def test_signal_net_pnl_reads_attr_and_dict(fee_cfg):
    settings.report_fees_enabled = True
    settings.report_roundtrip_fee_bps = 8.0

    class _Sig:
        pnl_pct = 2.0

    assert signal_net_pnl(_Sig()) == pytest.approx(1.92)
    assert signal_net_pnl({"pnl_pct": 2.0}) == pytest.approx(1.92)
    # Missing pnl_pct degrades to 0 net, not a crash.
    assert signal_net_pnl({}) == 0.0
