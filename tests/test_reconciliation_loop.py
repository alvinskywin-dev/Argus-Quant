"""
Periodic reconciliation sweep (live-safety #3).

Covers the pure critical-summary extraction and the sweep's alert gating:
admins are alerted only when *new* drift is persisted (not every interval), and
never when the sweep is clean. No real DB/exchange — the engine and session are
stubbed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.config import settings
from app.reconciliation import loop as recon_loop


def _result(*, critical=0, warning=0, created=0, users=1):
    """Build a reconcile_all_active_users-shaped result with N issues."""
    issues = []
    issues += [
        {"severity": "CRITICAL", "symbol": f"C{i}USDT", "issue_type": "SIDE_MISMATCH"}
        for i in range(critical)
    ]
    issues += [
        {"severity": "WARNING", "symbol": f"W{i}USDT", "issue_type": "SIZE_MISMATCH"}
        for i in range(warning)
    ]
    return {
        "users": users,
        "issues_found": critical + warning,
        "issues_created": created,
        "per_user": [
            {
                "user_id": 7,
                "results": [{"exchange": "binance", "issues": issues}],
            }
        ],
    }


# ── pure summary ──────────────────────────────────────────────────────────────
def test_summarize_critical_counts_and_lines():
    s = recon_loop.summarize_critical(_result(critical=2, warning=1))
    assert s["critical"] == 2
    assert s["warning"] == 1
    assert len(s["lines"]) == 3
    assert any(line.startswith("[CRITICAL]") for line in s["lines"])
    assert any("binance" in line for line in s["lines"])


def test_summarize_critical_clean():
    s = recon_loop.summarize_critical(_result())
    assert s == {"critical": 0, "warning": 0, "lines": []}


# ── sweep alert gating ────────────────────────────────────────────────────────
@pytest.fixture
def stub_engine(monkeypatch):
    """Stub get_session + reconcile_all_active_users; capture alert calls."""

    @asynccontextmanager
    async def _fake_session():
        yield object()

    monkeypatch.setattr("app.database.session.get_session", _fake_session)

    calls: list = []

    def _set_result(result):
        async def _fake_recon(db, persist=True):
            return result

        monkeypatch.setattr("app.reconciliation.engine.reconcile_all_active_users", _fake_recon)

    async def _alert(title, body):
        calls.append((title, body))

    return _set_result, _alert, calls


@pytest.mark.asyncio
async def test_sweep_alerts_on_new_critical(stub_engine):
    set_result, alert, calls = stub_engine
    set_result(_result(critical=1, created=1))
    await recon_loop.run_reconciliation_sweep(alert=alert)
    assert len(calls) == 1
    assert "1 critical" in calls[0][0]


@pytest.mark.asyncio
async def test_sweep_does_not_alert_when_nothing_new(stub_engine):
    # Critical drift exists but nothing was newly persisted (standing issue) ->
    # no re-alert, avoiding per-interval spam.
    set_result, alert, calls = stub_engine
    set_result(_result(critical=1, created=0))
    await recon_loop.run_reconciliation_sweep(alert=alert)
    assert calls == []


@pytest.mark.asyncio
async def test_sweep_clean_no_alert(stub_engine):
    set_result, alert, calls = stub_engine
    set_result(_result(created=0))
    await recon_loop.run_reconciliation_sweep(alert=alert)
    assert calls == []


@pytest.mark.asyncio
async def test_sweep_respects_alert_disabled(stub_engine, monkeypatch):
    set_result, alert, calls = stub_engine
    set_result(_result(critical=2, created=2))
    monkeypatch.setattr(settings, "reconciliation_alert_critical", False)
    await recon_loop.run_reconciliation_sweep(alert=alert)
    assert calls == []


# ── loop is a no-op when disabled ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_loop_returns_immediately_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "reconciliation_loop_enabled", False)
    # Must return promptly rather than entering the forever-loop.
    await recon_loop.reconciliation_loop(alert=None)
