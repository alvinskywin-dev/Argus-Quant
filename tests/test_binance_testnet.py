"""Phase 21F — Binance testnet config guards (no network).

Pins the safety contract of the testnet preflight: it refuses unless testnet is
enabled, keys are present, and the base URL is the testnet host — and the
adapter resolves to the testnet REST base. No mainnet URL is ever accepted.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.exchange_adapters.binance import _PROD_URL, _TESTNET_URL, BinanceFuturesAdapter
from app.exchange_vault.binance_preflight import _PROD_FAPI, _TESTNET_FAPI
from app.exchange_vault.binance_testnet import (
    BinanceTestnetGuardError,
    is_testnet_url,
    resolve_testnet_config,
)


@pytest.fixture
def tcfg():
    """Save/restore the testnet settings around a test."""
    keys = (
        "binance_testnet_enabled",
        "binance_testnet_base_url",
        "binance_testnet_api_key",
        "binance_testnet_api_secret",
    )
    saved = {k: getattr(settings, k) for k in keys}
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


def _ok(cfg):
    cfg.binance_testnet_enabled = True
    cfg.binance_testnet_base_url = _TESTNET_FAPI
    cfg.binance_testnet_api_key = "tkey"
    cfg.binance_testnet_api_secret = "tsecret"


def test_disabled_refuses_script(tcfg):
    _ok(tcfg)
    tcfg.binance_testnet_enabled = False
    with pytest.raises(BinanceTestnetGuardError):
        resolve_testnet_config()


def test_missing_keys_refuses_script(tcfg):
    _ok(tcfg)
    tcfg.binance_testnet_api_key = ""
    with pytest.raises(BinanceTestnetGuardError):
        resolve_testnet_config()
    _ok(tcfg)
    tcfg.binance_testnet_api_secret = ""
    with pytest.raises(BinanceTestnetGuardError):
        resolve_testnet_config()


def test_non_testnet_url_refuses_script(tcfg):
    _ok(tcfg)
    tcfg.binance_testnet_base_url = _PROD_FAPI  # mainnet — must be refused
    with pytest.raises(BinanceTestnetGuardError):
        resolve_testnet_config()


def test_adapter_picks_testnet_url():
    a = BinanceFuturesAdapter("k", "s", testnet=True)
    assert a._base == _TESTNET_URL
    b = BinanceFuturesAdapter("k", "s", testnet=False)
    assert b._base == _PROD_URL


def test_validator_returns_testnet_mode(tcfg):
    _ok(tcfg)
    cfg = resolve_testnet_config()
    assert cfg.mode == "TESTNET"
    assert is_testnet_url(cfg.base_url)
    assert cfg.api_key == "tkey" and cfg.api_secret == "tsecret"


def test_no_mainnet_url_in_testnet_mode():
    assert is_testnet_url(_TESTNET_FAPI) is True
    assert is_testnet_url(_PROD_FAPI) is False
    assert is_testnet_url("https://fapi.binance.com") is False
    assert is_testnet_url("https://testnet.binancefuture.com") is True
