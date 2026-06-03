"""
Sprint 20C — AES-256-GCM encryption for exchange credentials.

The 256-bit key is derived from the configured master secret via HKDF-SHA256,
so the raw env secret is never used directly as a key. Every encryption uses a
fresh 96-bit nonce; the stored token is base64(nonce || ciphertext||tag).
Plaintext secrets are never persisted.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import settings

_NONCE_BYTES = 12
_KEY_BYTES = 32  # AES-256
_HKDF_SALT = b"alpha-radar-exchange-vault-v1"
_HKDF_INFO = b"exchange-credential-encryption"


class VaultCryptoError(Exception):
    """Raised when encryption/decryption fails or the vault key is unusable."""


def _derive_key() -> bytes:
    material = settings.vault_key_material
    if not material:
        raise VaultCryptoError(
            "No vault key configured. Set VAULT_MASTER_KEY or SECRET_KEY."
        )
    hkdf = HKDF(algorithm=SHA256(), length=_KEY_BYTES, salt=_HKDF_SALT, info=_HKDF_INFO)
    return hkdf.derive(material.encode("utf-8"))


def encrypt(plaintext: str) -> str:
    """Return base64(nonce || ciphertext) for `plaintext`."""
    if plaintext is None:
        raise VaultCryptoError("cannot encrypt None")
    key = _derive_key()
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(token: str) -> str:
    """Reverse of encrypt(). Raises VaultCryptoError on tamper / wrong key."""
    if not token:
        raise VaultCryptoError("empty token")
    try:
        raw = base64.b64decode(token.encode("ascii"))
        nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(_derive_key()).decrypt(nonce, ct, None).decode("utf-8")
    except VaultCryptoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VaultCryptoError("decryption failed (bad key or corrupted data)") from exc


def encrypt_optional(plaintext: str | None) -> str | None:
    return encrypt(plaintext) if plaintext else None
