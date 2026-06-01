"""
Sprint 20C — unit tests for vault crypto + mock validation (no DB required).
"""
from __future__ import annotations

import pytest

from app.exchange_vault import crypto
from app.exchange_vault.adapters import (
    MockExchangeValidator,
    get_validator,
)


# ── AES-256-GCM crypto ────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    secret = "my-super-secret-api-key-1234567890"
    token = crypto.encrypt(secret)
    assert token != secret
    assert crypto.decrypt(token) == secret


def test_ciphertext_is_nondeterministic():
    # Fresh nonce each time -> different ciphertext for same plaintext.
    a = crypto.encrypt("same-value")
    b = crypto.encrypt("same-value")
    assert a != b
    assert crypto.decrypt(a) == crypto.decrypt(b) == "same-value"


def test_decrypt_tampered_token_fails():
    token = crypto.encrypt("value")
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    with pytest.raises(crypto.VaultCryptoError):
        crypto.decrypt(tampered)


def test_decrypt_empty_fails():
    with pytest.raises(crypto.VaultCryptoError):
        crypto.decrypt("")


def test_encrypt_optional():
    assert crypto.encrypt_optional(None) is None
    assert crypto.encrypt_optional("") is None
    tok = crypto.encrypt_optional("x")
    assert tok and crypto.decrypt(tok) == "x"


# ── mock validator permission inference ───────────────────────────

def test_mock_normal_key_is_trade_futures_no_withdraw():
    p = MockExchangeValidator("binance").validate("GOODKEY123", "secret", None)
    assert p.valid and p.can_trade and p.can_futures and not p.can_withdraw


def test_mock_withdrawal_key_flagged():
    p = MockExchangeValidator("binance").validate("WITHDRAWkey", "secret", None)
    assert p.valid and p.can_withdraw
    p2 = MockExchangeValidator("binance").validate("WDsomething", "secret", None)
    assert p2.can_withdraw


def test_mock_notrade_nofutures_badkey():
    assert not MockExchangeValidator("binance").validate("NOTRADEx", "s", None).can_trade
    assert not MockExchangeValidator("binance").validate("NOFUTURESx", "s", None).can_futures
    assert not MockExchangeValidator("binance").validate("BADKEYx", "s", None).valid


def test_mock_missing_secret_invalid():
    assert not MockExchangeValidator("binance").validate("KEY", "", None).valid


def test_mock_passphrase_required_for_okx_bitget():
    assert not MockExchangeValidator("okx").validate("KEY", "secret", None).valid
    assert MockExchangeValidator("okx").validate("KEY", "secret", "pass").valid
    assert not MockExchangeValidator("bitget").validate("KEY", "secret", None).valid


def test_get_validator_supported_and_unsupported(monkeypatch):
    # Force MOCK so the test is independent of the deployment's live gate.
    from app.config import settings
    monkeypatch.setattr(settings, "mock_exchange_mode", True)
    assert get_validator("binance").name == "mock"
    with pytest.raises(ValueError):
        get_validator("ftx")
