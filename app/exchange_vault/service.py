"""
Sprint 20C — exchange vault business logic.

Stores per-user exchange credentials as AES-256-GCM ciphertext only. Connect
and test validate trading + futures permission via an adapter and REJECT any
key that has withdrawal enabled (such a key is never persisted). Every action
is written to the exchange_audit_log.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ExchangeAccount, ExchangeAuditLog
from app.database.session import SessionLocal
from app.exchange_vault import crypto
from app.exchange_vault.permission_validator import (
    STATUS_CONNECTED,
    STATUS_INVALID,
    STATUS_IP_RESTRICTED,
    STATUS_PERMISSION_DENIED,
    STATUS_VALIDATION_UNAVAILABLE,
    ExchangePermissionResult,
    validate_permissions,
)
from app.utils.logger import logger


class VaultError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _audit(
    db: AsyncSession, user_id: int, exchange: str, action: str, result: str,
    detail: str = "", ip: Optional[str] = None,
) -> None:
    """
    Write an audit row in its OWN session and commit immediately, so the record
    survives even when the caller's transaction rolls back on a rejection. The
    `db` argument is accepted for signature symmetry but intentionally unused.
    """
    try:
        async with SessionLocal() as audit_db:
            audit_db.add(
                ExchangeAuditLog(
                    user_id=user_id, exchange=exchange, action=action,
                    result=result, detail=(detail or "")[:256], ip=ip,
                )
            )
            await audit_db.commit()
    except Exception as exc:  # noqa: BLE001 — audit must never break the flow
        logger.warning(f"[vault] audit write failed: {exc}")


async def _get(db: AsyncSession, user_id: int, exchange: str, label: str) -> Optional[ExchangeAccount]:
    res = await db.execute(
        select(ExchangeAccount).where(
            ExchangeAccount.user_id == user_id,
            ExchangeAccount.exchange == exchange,
            ExchangeAccount.label == label,
        )
    )
    return res.scalar_one_or_none()


def _status_to_vault_error(r: ExchangePermissionResult) -> VaultError:
    """Map a non-CONNECTED validation outcome to the right HTTP error."""
    detail = r.permission_warning or r.error_message or "API key validation failed"
    if r.status == STATUS_INVALID:
        return VaultError(400, f"Invalid API credentials. {r.error_message}".strip())
    if r.status == STATUS_IP_RESTRICTED:
        return VaultError(403, "API key is IP-restricted and this server's IP is not allowed. "
                               "Whitelist the server IP or use an unrestricted trade-only key.")
    if r.status == STATUS_VALIDATION_UNAVAILABLE:
        return VaultError(503, "Could not validate the API key right now. Please try again. "
                               f"{r.error_message}".strip())
    if r.status == STATUS_PERMISSION_DENIED and r.can_withdraw:
        return VaultError(403, "Withdrawal-enabled API keys are not allowed. "
                               "Create a key with trading-only permission.")
    if r.status == STATUS_PERMISSION_DENIED:
        return VaultError(400, detail)
    return VaultError(400, detail)


# ── connect ───────────────────────────────────────────────────────

async def connect(
    db: AsyncSession,
    *,
    user_id: int,
    exchange: str,
    api_key: str,
    api_secret: str,
    passphrase: Optional[str],
    label: str = "default",
    ip: Optional[str] = None,
) -> ExchangeAccount:
    exchange = exchange.lower()

    # Sprint 21A — real, read-only permission validation BEFORE anything is
    # stored. A key is only ever persisted as CONNECTED when validation passes
    # (valid + trade + futures + NOT withdrawal-enabled). Withdrawal-enabled or
    # invalid keys are rejected and never written to the vault.
    result = await validate_permissions(exchange, api_key, api_secret, passphrase)
    if result.status != STATUS_CONNECTED:
        exc = _status_to_vault_error(result)
        action = "REJECT" if result.can_withdraw else "CONNECT"
        await _audit(db, user_id, exchange, action, "REJECTED",
                     f"{result.status}: {exc.detail}", ip)
        raise exc

    acc = await _get(db, user_id, exchange, label)
    if acc is None:
        acc = ExchangeAccount(user_id=user_id, exchange=exchange, label=label)
        db.add(acc)

    # Only ciphertext is ever written.
    acc.encrypted_api_key = crypto.encrypt(api_key)
    acc.encrypted_api_secret = crypto.encrypt(api_secret)
    acc.encrypted_passphrase = crypto.encrypt_optional(passphrase)
    acc.api_key_last4 = api_key[-4:]
    acc.status = "CONNECTED"
    acc.can_read = result.can_read
    acc.can_trade = result.can_trade
    acc.can_futures = result.can_futures
    acc.can_withdraw = False  # guaranteed: a withdraw-enabled key never reaches here
    acc.last_validation_status = result.status
    acc.permission_warning = (result.permission_warning or None)
    acc.last_error = None
    acc.last_test = _now()

    await db.flush()
    await _audit(db, user_id, exchange, "CONNECT", "OK", f"label={label}", ip)
    logger.info(f"[vault] connected user={user_id} {exchange}/{label} status={result.status}")
    return acc


# ── test ──────────────────────────────────────────────────────────

async def test_connection(
    db: AsyncSession, *, user_id: int, exchange: str, label: str = "default",
    ip: Optional[str] = None,
) -> tuple[ExchangeAccount, ExchangePermissionResult]:
    exchange = exchange.lower()
    acc = await _get(db, user_id, exchange, label)
    if acc is None or not acc.encrypted_api_key:
        raise VaultError(404, "No connected account for that exchange")

    try:
        api_key = crypto.decrypt(acc.encrypted_api_key)
        api_secret = crypto.decrypt(acc.encrypted_api_secret or "")
        passphrase = crypto.decrypt(acc.encrypted_passphrase) if acc.encrypted_passphrase else None
    except crypto.VaultCryptoError as exc:
        acc.status = "ERROR"
        acc.last_error = "decrypt failed"
        await _audit(db, user_id, exchange, "TEST", "FAIL", str(exc), ip)
        raise VaultError(500, "Stored credentials could not be decrypted")

    result = await validate_permissions(exchange, api_key, api_secret, passphrase)
    acc.last_test = _now()
    acc.can_read = result.can_read
    acc.can_trade = result.can_trade
    acc.can_futures = result.can_futures
    acc.can_withdraw = bool(result.can_withdraw)   # None (undetectable) -> False
    acc.last_validation_status = result.status
    acc.permission_warning = (result.permission_warning or None)

    if result.status == STATUS_CONNECTED:
        acc.status = "CONNECTED"
        acc.last_error = None
        await _audit(db, user_id, exchange, "TEST", "OK", result.permission_warning or "", ip)
    elif result.status == STATUS_PERMISSION_DENIED and result.can_withdraw:
        # A previously-safe key gained withdrawal rights — quarantine it.
        acc.status = "ERROR"
        acc.last_error = "withdrawal permission detected"
        await _audit(db, user_id, exchange, "TEST", "REJECTED", "withdrawal enabled", ip)
    else:
        # INVALID / PERMISSION_DENIED / IP_RESTRICTED / VALIDATION_UNAVAILABLE / ERROR
        acc.status = "ERROR"
        acc.last_error = (result.error_message or result.permission_warning or result.status)[:256]
        await _audit(db, user_id, exchange, "TEST", "FAIL", f"{result.status}: {acc.last_error}", ip)
    return acc, result


# ── disconnect ────────────────────────────────────────────────────

async def disconnect(
    db: AsyncSession, *, user_id: int, exchange: str, label: str = "default",
    ip: Optional[str] = None,
) -> None:
    exchange = exchange.lower()
    acc = await _get(db, user_id, exchange, label)
    if acc is None:
        raise VaultError(404, "No account for that exchange")
    # Wipe ciphertext so no secret material is retained after disconnect.
    acc.encrypted_api_key = None
    acc.encrypted_api_secret = None
    acc.encrypted_passphrase = None
    acc.status = "DISCONNECTED"
    acc.can_trade = False
    acc.can_futures = False
    acc.can_withdraw = False
    acc.last_error = None
    await _audit(db, user_id, exchange, "DISCONNECT", "OK", f"label={label}", ip)
    logger.info(f"[vault] disconnected user={user_id} {exchange}/{label}")


# ── list ──────────────────────────────────────────────────────────

async def list_accounts(db: AsyncSession, user_id: int) -> list[ExchangeAccount]:
    res = await db.execute(
        select(ExchangeAccount)
        .where(ExchangeAccount.user_id == user_id)
        .order_by(ExchangeAccount.exchange, ExchangeAccount.label)
    )
    return list(res.scalars().all())


async def get_decrypted_credentials(
    db: AsyncSession, user_id: int, exchange: str, label: str = "default"
) -> dict:
    """
    Internal use only (auto-trading engine, 20D+). Returns decrypted creds.
    NEVER expose this through a public response model.
    """
    acc = await _get(db, user_id, exchange.lower(), label)
    if acc is None or acc.status != "CONNECTED" or not acc.encrypted_api_key:
        raise VaultError(404, "No connected account for that exchange")
    return {
        "exchange": acc.exchange,
        "api_key": crypto.decrypt(acc.encrypted_api_key),
        "api_secret": crypto.decrypt(acc.encrypted_api_secret or ""),
        "passphrase": crypto.decrypt(acc.encrypted_passphrase) if acc.encrypted_passphrase else None,
    }
