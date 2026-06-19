"""
Credential validation must be real even under MOCK_EXCHANGE_MODE.

Bug: connect/test reported an invalid key as CONNECTED because
validate_permissions short-circuited to offline mock inference whenever
mock_exchange_mode was on. mock_exchange_mode only governs ORDER simulation —
key validation must hit the exchange (read-only). validate_keys_live (default
True) enforces that; setting it False restores the old offline behaviour.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.exchange_vault import permission_validator as pv
from app.exchange_vault.permission_validator import (
    STATUS_CONNECTED,
    STATUS_INVALID,
    ExchangePermissionResult,
    validate_permissions,
)


@pytest.fixture
def restore_settings():
    saved = (settings.mock_exchange_mode, settings.validate_keys_live)
    yield
    settings.mock_exchange_mode, settings.validate_keys_live = saved


@pytest.mark.asyncio
async def test_invalid_key_rejected_in_mock_mode(monkeypatch, restore_settings):
    settings.mock_exchange_mode = True
    settings.validate_keys_live = True

    async def fake_validate_okx(api_key, api_secret, passphrase):
        return ExchangePermissionResult(
            exchange="okx", ok=False, status=STATUS_INVALID, error_message="bad key"
        )

    monkeypatch.setattr(pv, "validate_okx", fake_validate_okx)
    r = await validate_permissions("okx", "k", "s", "wrong-pass")
    # Real validator ran (not the always-pass mock) → invalid key is rejected.
    assert r.status == STATUS_INVALID


@pytest.mark.asyncio
async def test_valid_key_connected_in_mock_mode(monkeypatch, restore_settings):
    settings.mock_exchange_mode = True
    settings.validate_keys_live = True

    async def fake_validate_binance(api_key, api_secret):
        return ExchangePermissionResult(exchange="binance", ok=True, status=STATUS_CONNECTED)

    monkeypatch.setattr(pv, "validate_binance", fake_validate_binance)
    r = await validate_permissions("binance", "k", "s")
    assert r.status == STATUS_CONNECTED


@pytest.mark.asyncio
async def test_offline_inference_when_validate_keys_live_off(monkeypatch, restore_settings):
    # With the escape hatch off, the old offline mock path is used (no network).
    settings.mock_exchange_mode = True
    settings.validate_keys_live = False

    called = {"real": False}

    async def fake_validate_okx(*a, **k):
        called["real"] = True
        return ExchangePermissionResult(exchange="okx", ok=False, status=STATUS_INVALID)

    monkeypatch.setattr(pv, "validate_okx", fake_validate_okx)
    r = await validate_permissions("okx", "k", "s", "p")
    assert called["real"] is False  # real validator NOT called
    assert r.raw_safe_summary == {"mock": True}
