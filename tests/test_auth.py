"""
Sprint 20A — unit tests for auth primitives (no DB required).
"""

from __future__ import annotations

import pyotp
import pytest

from app.auth import security

# ── password hashing ──────────────────────────────────────────────


def test_password_hash_roundtrip():
    h = security.hash_password("CorrectHorse9!")
    assert h != "CorrectHorse9!"
    assert security.verify_password("CorrectHorse9!", h)


def test_password_wrong_rejected():
    h = security.hash_password("CorrectHorse9!")
    assert not security.verify_password("wrong-password", h)


def test_password_verify_handles_garbage_hash():
    assert not security.verify_password("anything", "not-a-real-bcrypt-hash")


def test_password_over_72_bytes_does_not_crash():
    long_pw = "a" * 200
    h = security.hash_password(long_pw)
    assert security.verify_password(long_pw, h)


# ── JWT access tokens ─────────────────────────────────────────────


def test_access_token_roundtrip():
    token = security.create_access_token(42, "PREMIUM")
    payload = security.decode_access_token(token)
    assert payload["sub"] == "42"
    assert payload["role"] == "PREMIUM"
    assert payload["type"] == "access"


def test_expired_access_token_rejected():
    token = security.create_access_token(1, "FREE", ttl_minutes=-1)
    with pytest.raises(security.TokenError):
        security.decode_access_token(token)


def test_tampered_token_rejected():
    token = security.create_access_token(1, "FREE")
    with pytest.raises(security.TokenError):
        security.decode_access_token(token + "x")


# ── opaque tokens ─────────────────────────────────────────────────


def test_opaque_tokens_unique_and_hashable():
    a = security.generate_opaque_token()
    b = security.generate_opaque_token()
    assert a != b
    assert security.hash_token(a) == security.hash_token(a)
    assert security.hash_token(a) != security.hash_token(b)
    assert len(security.hash_token(a)) == 64  # sha256 hex


# ── TOTP 2FA ──────────────────────────────────────────────────────


def test_totp_verify_accepts_current_code():
    secret = security.generate_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert security.verify_totp(secret, code)


def test_totp_rejects_bad_code():
    secret = security.generate_totp_secret()
    assert not security.verify_totp(secret, "000000")


def test_totp_rejects_empty():
    assert not security.verify_totp(None, "123456")
    assert not security.verify_totp("ABC", "")


def test_totp_provisioning_uri_contains_issuer():
    secret = security.generate_totp_secret()
    uri = security.totp_provisioning_uri(secret, "user@example.com")
    assert uri.startswith("otpauth://totp/")
    # account + issuer are URL-encoded in the otpauth URI
    assert "example.com" in uri
    assert f"secret={secret}" in uri
    assert "issuer=" in uri
