"""
Admin runtime live-trading switch.

A real order is permitted only when the admin runtime switch is ON *and* the hard
env floor (MOCK_EXCHANGE_MODE=false) allows it. Enabling via the admin service
requires a confirmation phrase. These guard real money, so they are unit-tested.
"""

from __future__ import annotations

import pytest

from app.admin import service as admin_service
from app.config import settings
from app.exchange_adapters import (
    live_gate_open,
    runtime_live_enabled,
    set_runtime_live_enabled,
)


@pytest.fixture
def restore_gate():
    saved = (settings.mock_exchange_mode, runtime_live_enabled())
    yield
    settings.mock_exchange_mode = saved[0]
    set_runtime_live_enabled(saved[1])


def test_mock_mode_keeps_gate_closed_even_when_runtime_on(restore_gate):
    settings.mock_exchange_mode = True
    set_runtime_live_enabled(True)
    assert live_gate_open() is False  # hard env floor wins


def test_runtime_off_keeps_gate_closed(restore_gate):
    settings.mock_exchange_mode = False
    set_runtime_live_enabled(False)
    assert live_gate_open() is False


def test_gate_open_only_when_both_allow(restore_gate):
    settings.mock_exchange_mode = False
    set_runtime_live_enabled(True)
    assert live_gate_open() is True


def test_runtime_flag_roundtrip(restore_gate):
    set_runtime_live_enabled(True)
    assert runtime_live_enabled() is True
    set_runtime_live_enabled(False)
    assert runtime_live_enabled() is False


@pytest.mark.asyncio
async def test_enable_requires_confirmation_phrase(restore_gate):
    set_runtime_live_enabled(False)
    # Wrong/empty confirm must raise BEFORE any state change (no DB needed).
    with pytest.raises(admin_service.AdminError):
        await admin_service.set_live_trading(None, admin_id=1, enabled=True, confirm="")
    assert runtime_live_enabled() is False  # unchanged
