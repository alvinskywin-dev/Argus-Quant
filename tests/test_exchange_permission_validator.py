"""
Sprint 21A — unit tests for the exchange permission validator.

Pure-classifier + mock-mapping tests only. No network and no DB: the real
``validate_*`` coroutines hit live exchanges and are never exercised in CI.
"""
from __future__ import annotations

from app.exchange_vault.adapters import MockExchangeValidator
from app.exchange_vault.permission_validator import (
    STATUS_CONNECTED,
    STATUS_INVALID,
    STATUS_PERMISSION_DENIED,
    classify_binance,
    classify_bitget,
    classify_bybit,
    classify_okx,
    finalize_status,
    from_mock_permissions,
    ExchangePermissionResult,
)


# ── Binance classifier ─────────────────────────────────────────────

def test_binance_trade_futures_no_withdraw_is_connected():
    r = classify_binance({
        "enableReading": True, "enableFutures": True,
        "enableSpotAndMarginTrading": True, "enableWithdrawals": False,
    })
    assert r.ok and r.status == STATUS_CONNECTED
    assert r.can_read and r.can_trade and r.can_futures and r.can_withdraw is False
    assert "FUTURES" in r.permissions


def test_binance_withdrawal_key_is_rejected():
    r = classify_binance({
        "enableReading": True, "enableFutures": True,
        "enableSpotAndMarginTrading": True, "enableWithdrawals": True,
    })
    assert r.status == STATUS_PERMISSION_DENIED
    assert r.can_withdraw is True
    assert "WITHDRAWAL" in r.permission_warning.upper()


def test_binance_no_futures_is_permission_denied():
    r = classify_binance({
        "enableReading": True, "enableFutures": False,
        "enableSpotAndMarginTrading": True, "enableWithdrawals": False,
    })
    assert r.status == STATUS_PERMISSION_DENIED
    assert not r.can_futures


def test_binance_no_trade_is_permission_denied():
    r = classify_binance({
        "enableReading": True, "enableFutures": False,
        "enableSpotAndMarginTrading": False, "enableWithdrawals": False,
    })
    assert r.status == STATUS_PERMISSION_DENIED
    assert not r.can_trade


# ── OKX classifier ─────────────────────────────────────────────────

def test_okx_trade_perm_connected_withdraw_known_false():
    r = classify_okx({"data": [{"perm": "read_only,trade", "acctLv": "3"}]})
    assert r.status == STATUS_CONNECTED
    assert r.can_trade and r.can_futures and r.can_withdraw is False


def test_okx_withdraw_perm_rejected():
    r = classify_okx({"data": [{"perm": "read_only,trade,withdraw", "acctLv": "3"}]})
    assert r.status == STATUS_PERMISSION_DENIED
    assert r.can_withdraw is True


def test_okx_missing_perm_field_marks_withdraw_undetectable():
    r = classify_okx({"data": [{"acctLv": "2"}]})
    # withdraw cannot be determined -> None, with a warning, but still connectable
    assert r.can_withdraw is None
    assert r.status == STATUS_CONNECTED
    assert "withdraw" in r.permission_warning.lower()


# ── Bybit classifier ───────────────────────────────────────────────

def test_bybit_contract_trade_connected():
    r = classify_bybit({"result": {
        "readOnly": 0,
        "permissions": {"ContractTrade": ["Order", "Position"], "Wallet": []},
    }})
    assert r.status == STATUS_CONNECTED
    assert r.can_futures and r.can_trade and r.can_withdraw is False


def test_bybit_withdraw_permission_rejected():
    r = classify_bybit({"result": {
        "readOnly": 0,
        "permissions": {"ContractTrade": ["Order"], "Wallet": ["AccountTransfer", "Withdraw"]},
    }})
    assert r.status == STATUS_PERMISSION_DENIED
    assert r.can_withdraw is True


def test_bybit_read_only_key_has_no_trade():
    r = classify_bybit({"result": {
        "readOnly": 1,
        "permissions": {"ContractTrade": ["Order"]},
    }})
    assert not r.can_trade
    assert r.status == STATUS_PERMISSION_DENIED


# ── Bitget classifier ──────────────────────────────────────────────

def test_bitget_reachable_account_connected_withdraw_undetectable():
    r = classify_bitget({"code": "00000", "data": [{"marginCoin": "USDT"}]})
    assert r.status == STATUS_CONNECTED
    assert r.can_futures and r.can_withdraw is None
    assert r.permission_warning  # warns withdraw is undetectable


def test_bitget_unreachable_account_denied():
    r = classify_bitget({"code": "40037", "data": None})
    assert r.status == STATUS_PERMISSION_DENIED
    assert not r.ok


# ── finalize_status invariants ─────────────────────────────────────

def test_finalize_does_not_touch_failed_reads():
    r = ExchangePermissionResult(exchange="binance", ok=False, status=STATUS_INVALID)
    out = finalize_status(r)
    assert out.status == STATUS_INVALID


def test_withdraw_true_always_beats_trade_futures():
    r = ExchangePermissionResult(
        exchange="x", ok=True, can_read=True, can_trade=True,
        can_futures=True, can_withdraw=True)
    assert finalize_status(r).status == STATUS_PERMISSION_DENIED


# ── mock mapping (drives the offline validator the same way connect does) ──

def test_from_mock_normal_key_connected():
    p = MockExchangeValidator("binance").validate("GOODKEY123", "secret", None)
    r = from_mock_permissions("binance", p)
    assert r.status == STATUS_CONNECTED and r.can_trade and r.can_futures


def test_from_mock_withdraw_key_rejected():
    p = MockExchangeValidator("binance").validate("WITHDRAWkey", "secret", None)
    r = from_mock_permissions("binance", p)
    assert r.status == STATUS_PERMISSION_DENIED and r.can_withdraw is True


def test_from_mock_badkey_invalid():
    p = MockExchangeValidator("binance").validate("BADKEYx", "secret", None)
    r = from_mock_permissions("binance", p)
    assert r.status == STATUS_INVALID and not r.ok
