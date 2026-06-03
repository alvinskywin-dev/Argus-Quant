"""
Tests for the duplicate active-signal guard.

All DB calls are mocked — no live database required.
Run with:  pytest tests/test_duplicate_guard.py -v
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to build minimal Signal-like objects
# ---------------------------------------------------------------------------


def _sig(status: str, symbol: str = "BTCUSDT", side: str = "LONG", sig_id: int = 1):
    """Return a lightweight namespace that mimics Signal columns used by the guard."""
    s = MagicMock()
    s.id = sig_id
    s.symbol = symbol
    s.side = side
    s.status = status
    s.closed_at = datetime.now(timezone.utc) if status in ("TP1", "TP2", "TP3", "SL") else None
    s.created_at = datetime.now(timezone.utc)
    return s


# ---------------------------------------------------------------------------
# Mock session factory
# ---------------------------------------------------------------------------


def _make_session(scalar_result: Any):
    """
    Build an AsyncContextManager mock whose execute() returns a result with
    scalar_one_or_none() == scalar_result.
    """
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = scalar_result
    result_mock.all.return_value = []

    session_mock = AsyncMock()
    session_mock.execute = AsyncMock(return_value=result_mock)
    session_mock.commit = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield session_mock

    return _ctx


# ---------------------------------------------------------------------------
# has_active_signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_signal_blocks_new():
    """has_active_signal returns True when an OPEN row exists for that symbol."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=42)):
        from app.database.repo import has_active_signal

        assert await has_active_signal("BTCUSDT") is True


@pytest.mark.asyncio
async def test_closed_signal_allows_new():
    """has_active_signal returns False when no active row exists (signal is TP/SL)."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=None)):
        from app.database.repo import has_active_signal

        assert await has_active_signal("BTCUSDT") is False


@pytest.mark.asyncio
async def test_sl_signal_allows_new():
    """SL-closed signal → no active row → new signal is allowed."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=None)):
        from app.database.repo import has_active_signal

        assert await has_active_signal("ETHUSDT") is False


@pytest.mark.asyncio
async def test_tp_signal_allows_new():
    """TP1/TP2/TP3-closed signal → no active row → new signal is allowed."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=None)):
        from app.database.repo import has_active_signal

        assert await has_active_signal("SOLUSDT") is False


@pytest.mark.asyncio
async def test_opposite_side_blocked_by_symbol_guard():
    """When block_same_symbol_while_open is True, opposite-side is also blocked
    because the guard checks by symbol only (no side filter)."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=99)):
        from app.database.repo import has_active_signal

        # side=None means symbol-level guard — finds ANY active signal
        assert await has_active_signal("BTCUSDT", side=None) is True


@pytest.mark.asyncio
async def test_has_active_excluding_passes_for_own_id():
    """has_active_signal_excluding returns False when the only active signal
    is the one being excluded (the just-persisted signal itself)."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=None)):
        from app.database.repo import has_active_signal_excluding

        # scalar=None means no OTHER active signal found → publisher should not block
        assert await has_active_signal_excluding("BTCUSDT", exclude_id=7) is False


@pytest.mark.asyncio
async def test_has_active_excluding_blocks_on_other_open():
    """has_active_signal_excluding returns True when a DIFFERENT signal is open."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=5)):
        from app.database.repo import has_active_signal_excluding

        # scalar=5 means another signal (ID≠7) is OPEN → publisher must block
        assert await has_active_signal_excluding("BTCUSDT", exclude_id=7) is True


# ---------------------------------------------------------------------------
# in_post_close_cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_close_cooldown_disabled_when_zero():
    """Setting cooldown_hours=0 always returns False (feature disabled)."""
    from app.database.repo import in_post_close_cooldown

    # Should return False without touching the DB
    result = await in_post_close_cooldown("BTCUSDT", "LONG", hours=0)
    assert result is False


@pytest.mark.asyncio
async def test_post_close_cooldown_active_when_recent_close():
    """Returns True if a recent TP/SL close is within the cooldown window."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=3)):
        from app.database.repo import in_post_close_cooldown

        assert await in_post_close_cooldown("BTCUSDT", "LONG", hours=24) is True


@pytest.mark.asyncio
async def test_post_close_cooldown_clear_when_no_recent_close():
    """Returns False if no recent close within the cooldown window."""
    with patch("app.database.repo.get_session", _make_session(scalar_result=None)):
        from app.database.repo import in_post_close_cooldown

        assert await in_post_close_cooldown("BTCUSDT", "LONG", hours=24) is False
