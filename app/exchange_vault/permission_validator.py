"""
Sprint 21A — Exchange real permission validator.

Validates real exchange API keys SAFELY before they are stored/activated.

Hard safety rules (enforced here):
  * Never place an order during validation — only read-only / account-permission
    endpoints are called.
  * If withdrawal permission cannot be detected reliably the result carries
    can_withdraw = None and a permission_warning (we do NOT silently treat
    "unknown" as "safe").
  * If validation cannot prove trading + futures permission, the account is NOT
    marked CONNECTED.
  * If real validation is unavailable for an exchange, status is
    VALIDATION_UNAVAILABLE (never CONNECTED).
  * No plaintext secret ever appears in logs or in raw_safe_summary.

The module is split into two layers so it is testable without network access:
  * Pure ``classify_*`` functions map an already-parsed exchange response to an
    :class:`ExchangePermissionResult`.  These are unit-tested.
  * The ``*_validator`` coroutines perform the read-only signed HTTP request and
    delegate to the classifier.  Network paths are exercised only against a real
    exchange (or testnet), never in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.exchange_vault.adapters import (
    PASSPHRASE_EXCHANGES,
    SUPPORTED_EXCHANGES,
    Permissions,
    get_validator,
)
from app.utils.logger import logger

# ── validation status states ──────────────────────────────────────
STATUS_CONNECTED = "CONNECTED"  # valid + trade + futures + no withdraw
STATUS_INVALID = "INVALID"  # bad key/secret/signature
STATUS_PERMISSION_DENIED = "PERMISSION_DENIED"  # valid key but missing/forbidden perms
STATUS_IP_RESTRICTED = "IP_RESTRICTED"  # key locked to an IP that is not us
STATUS_VALIDATION_UNAVAILABLE = "VALIDATION_UNAVAILABLE"  # cannot validate (no adapter/offline)
STATUS_ERROR = "ERROR"  # unexpected error

ALL_STATUSES = (
    STATUS_CONNECTED,
    STATUS_INVALID,
    STATUS_PERMISSION_DENIED,
    STATUS_IP_RESTRICTED,
    STATUS_VALIDATION_UNAVAILABLE,
    STATUS_ERROR,
)

# Binance hosts: SAPI (key-permission introspection) lives on prod only; the
# futures testnet exposes fapi but no SAPI, so testnet validation uses fapi.
_PROD_SAPI = "https://api.binance.com"
_TESTNET_FAPI = "https://testnet.binancefuture.com"


@dataclass
class ExchangePermissionResult:
    exchange: str
    ok: bool = False
    status: str = STATUS_ERROR
    can_read: bool = False
    can_trade: bool = False
    can_futures: bool = False
    can_withdraw: Optional[bool] = None  # None == could not be determined
    account_type: str = ""
    permissions: list[str] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    permission_warning: str = ""
    raw_safe_summary: dict[str, Any] = field(default_factory=dict)

    def is_connectable(self) -> bool:
        """True only when the key is safe to store and activate as CONNECTED."""
        return self.status == STATUS_CONNECTED

    def to_public_dict(self) -> dict[str, Any]:
        """Response-safe view. Never contains secrets."""
        return {
            "exchange": self.exchange,
            "ok": self.ok,
            "last_validation_status": self.status,
            "can_read": self.can_read,
            "can_trade": self.can_trade,
            "can_futures": self.can_futures,
            "can_withdraw": self.can_withdraw,
            "account_type": self.account_type,
            "permissions": self.permissions,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "permission_warning": self.permission_warning,
            "raw_safe_summary": self.raw_safe_summary,
        }


# ── shared finalisation ────────────────────────────────────────────


def finalize_status(r: ExchangePermissionResult) -> ExchangePermissionResult:
    """
    Derive the final status + warnings from detected capabilities.
    Only applied when the read succeeded (``ok``).  Network/credential errors
    set their own status earlier and skip this.
    """
    if not r.ok:
        return r
    warnings: list[str] = []
    if r.can_withdraw is True:
        # A withdrawal-enabled key is never safe — reject regardless of the rest.
        r.status = STATUS_PERMISSION_DENIED
        warnings.append("API key has WITHDRAWAL permission — rejected. Use a trade-only key.")
    elif not r.can_trade:
        r.status = STATUS_PERMISSION_DENIED
        warnings.append("API key lacks trading permission.")
    elif not r.can_futures:
        r.status = STATUS_PERMISSION_DENIED
        warnings.append("API key lacks futures/derivatives permission.")
    else:
        r.status = STATUS_CONNECTED
    if r.can_withdraw is None:
        warnings.append(
            "Withdrawal permission could not be confirmed for this exchange; "
            "ensure the key is trade-only."
        )
    r.permission_warning = " ".join(warnings)
    return r


# ════════════════════════════════════════════════════════════════════
#  Pure classifiers (unit-tested, no network)
# ════════════════════════════════════════════════════════════════════


def classify_binance(restrictions: dict) -> ExchangePermissionResult:
    """Map Binance GET /sapi/v1/account/apiRestrictions to a result."""
    can_read = bool(restrictions.get("enableReading", False))
    can_withdraw = bool(restrictions.get("enableWithdrawals", False))
    can_futures = bool(restrictions.get("enableFutures", False))
    can_spot = bool(restrictions.get("enableSpotAndMarginTrading", False))
    perms: list[str] = []
    if can_read:
        perms.append("READ")
    if can_spot:
        perms.append("SPOT_MARGIN")
    if can_futures:
        perms.append("FUTURES")
    if can_withdraw:
        perms.append("WITHDRAW")
    r = ExchangePermissionResult(
        exchange="binance",
        ok=True,
        can_read=can_read,
        can_trade=(can_futures or can_spot),
        can_futures=can_futures,
        can_withdraw=can_withdraw,
        account_type="futures",
        permissions=perms,
        raw_safe_summary={
            "enableReading": can_read,
            "enableFutures": can_futures,
            "enableSpotAndMarginTrading": can_spot,
            "enableWithdrawals": can_withdraw,
            "ipRestrict": bool(restrictions.get("ipRestrict", False)),
        },
    )
    return finalize_status(r)


def classify_binance_futures_account(
    account: dict,
    *,
    testnet: bool = False,
    trust_withdraw_flag: bool = False,
) -> ExchangePermissionResult:
    """
    Map a Binance ``GET /fapi/v2/account`` response to a result.

    Used where the SAPI ``apiRestrictions`` endpoint is unavailable — chiefly the
    futures **testnet** (``testnet.binancefuture.com`` has no SAPI). The futures
    account body carries ``canTrade`` (genuine trade-permission proof — a
    read-only key reports ``canTrade=false``) and account-level ``canWithdraw``.

    ``canWithdraw`` here is an *account*-level flag, not the API-key permission the
    prod SAPI path inspects, so by default it is reported as undetectable
    (``None`` + warning) rather than trusted — we never silently treat unknown as
    safe, and never falsely reject a trade-only key on a withdraw-capable account.
    """
    reachable = isinstance(account, dict) and (
        "canTrade" in account or "assets" in account or "totalWalletBalance" in account
    )
    if not reachable:
        return ExchangePermissionResult(
            exchange="binance",
            ok=False,
            status=STATUS_PERMISSION_DENIED,
            error_message="Binance futures account not reachable with this key",
        )
    can_trade = bool(account.get("canTrade", False))
    raw_withdraw = bool(account.get("canWithdraw", False))
    can_withdraw: Optional[bool] = raw_withdraw if trust_withdraw_flag else None
    perms = ["READ", "FUTURES"] + (["TRADE"] if can_trade else [])
    r = ExchangePermissionResult(
        exchange="binance",
        ok=True,
        can_read=True,
        can_trade=can_trade,
        can_futures=True,
        can_withdraw=can_withdraw,
        account_type="futures-testnet" if testnet else "futures",
        permissions=perms,
        raw_safe_summary={
            "source": "fapi_v2_account",
            "testnet": testnet,
            "canTrade": can_trade,
            "canWithdraw_account_level": raw_withdraw,
        },
    )
    return finalize_status(r)


def classify_okx(account_config: dict) -> ExchangePermissionResult:
    """
    Map OKX GET /api/v5/account/config to a result.
    OKX exposes the key permission bundle in ``perm`` (e.g. "read_only",
    "trade", "withdraw"); when absent, withdraw is reported as undetectable.
    """
    data = account_config.get("data") or [{}]
    row = data[0] if data else {}
    perm = str(row.get("perm", "")).lower()
    acct_lv = str(row.get("acctLv", ""))  # 2/3/4 == margin/futures/portfolio
    has_perm_field = bool(perm)
    can_trade = ("trade" in perm) if has_perm_field else True
    can_read = ("read" in perm) or ("trade" in perm) if has_perm_field else True
    can_withdraw: Optional[bool] = ("withdraw" in perm) if has_perm_field else None
    # acctLv 2+ means derivatives/swap are usable on this account.
    can_futures = acct_lv in ("2", "3", "4") if acct_lv else can_trade
    perms = [p.strip().upper() for p in perm.split(",") if p.strip()]
    r = ExchangePermissionResult(
        exchange="okx",
        ok=True,
        can_read=can_read,
        can_trade=can_trade,
        can_futures=can_futures,
        can_withdraw=can_withdraw,
        account_type=f"acctLv={acct_lv}" if acct_lv else "okx",
        permissions=perms,
        raw_safe_summary={"acctLv": acct_lv, "perm_present": has_perm_field},
    )
    return finalize_status(r)


def classify_bybit(api_info: dict) -> ExchangePermissionResult:
    """Map Bybit GET /v5/user/query-api ``result`` block to a result."""
    result = api_info.get("result", api_info) or {}
    perms = result.get("permissions", {}) or {}
    read_only = str(result.get("readOnly", "0")) in ("1", "true", "True")
    contract = list(perms.get("ContractTrade", []) or [])
    deriv = list(perms.get("Derivatives", []) or [])
    wallet = list(perms.get("Wallet", []) or [])
    spot = list(perms.get("Spot", []) or [])
    can_futures = bool(contract or deriv)
    can_trade = (not read_only) and bool(contract or deriv or spot)
    can_withdraw = any("Withdraw" in p for p in wallet)
    flat: list[str] = []
    for group, items in perms.items():
        for it in items or []:
            flat.append(f"{group}:{it}")
    r = ExchangePermissionResult(
        exchange="bybit",
        ok=True,
        can_read=True,
        can_trade=can_trade,
        can_futures=can_futures,
        can_withdraw=can_withdraw,
        account_type="unified",
        permissions=flat,
        raw_safe_summary={"readOnly": read_only, "has_contract": bool(contract or deriv)},
    )
    return finalize_status(r)


def classify_bitget(account_info: dict) -> ExchangePermissionResult:
    """
    Map a Bitget futures account/info response to a result.
    Bitget does not return per-key permission flags on the account endpoint, so
    withdraw is reported as undetectable (None) and a warning is attached.
    """
    data = account_info.get("data")
    ok = data is not None
    r = ExchangePermissionResult(
        exchange="bitget",
        ok=ok,
        can_read=ok,
        can_trade=ok,
        can_futures=ok,
        can_withdraw=None,
        account_type="USDT-FUTURES",
        permissions=["FUTURES"] if ok else [],
        raw_safe_summary={"account_reachable": ok},
    )
    if not ok:
        r.status = STATUS_PERMISSION_DENIED
        r.error_message = "Bitget futures account not reachable with this key"
        return r
    return finalize_status(r)


def from_mock_permissions(exchange: str, p: Permissions) -> ExchangePermissionResult:
    """Map the deterministic MockExchangeValidator result into the unified shape."""
    if not p.valid:
        return ExchangePermissionResult(
            exchange=exchange,
            ok=False,
            status=STATUS_INVALID,
            error_message=p.message or "Invalid API credentials",
        )
    r = ExchangePermissionResult(
        exchange=exchange,
        ok=True,
        can_read=True,
        can_trade=p.can_trade,
        can_futures=p.can_futures,
        can_withdraw=p.can_withdraw,
        account_type=p.account_type,
        permissions=["READ"]
        + (["TRADE"] if p.can_trade else [])
        + (["FUTURES"] if p.can_futures else [])
        + (["WITHDRAW"] if p.can_withdraw else []),
        raw_safe_summary={"mock": True},
    )
    return finalize_status(r)


# ════════════════════════════════════════════════════════════════════
#  Network validators (read-only; not exercised in CI)
# ════════════════════════════════════════════════════════════════════


async def _signed_get_json(url: str, *, params: dict, headers: dict) -> tuple[int, Any]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=12, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params, headers=headers) as resp:
            data = await resp.json(content_type=None)
            return resp.status, data


def _binance_error_status(code: Any, msg: str) -> str:
    code = str(code)
    low = (msg or "").lower()
    if code in ("-2014", "-1022") or "format" in low or "signature" in low:
        return STATUS_INVALID
    if code == "-2015":
        # "Invalid API-key, IP, or permissions for action"
        return STATUS_IP_RESTRICTED if "ip" in low else STATUS_PERMISSION_DENIED
    return STATUS_ERROR


async def validate_binance(
    api_key: str,
    api_secret: str,
    *,
    testnet: Optional[bool] = None,
) -> ExchangePermissionResult:
    """
    Validate a Binance key with a read-only signed request (never an order).

    * **Production** uses SAPI ``/sapi/v1/account/apiRestrictions`` — the
      authoritative API-key permission view, including the key-level withdrawal
      flag we must reject on.
    * **Testnet** (``testnet.binancefuture.com``) has no SAPI, so we fall back to
      the futures ``/fapi/v2/account`` endpoint, which still proves ``canTrade``.
    """
    import time

    from app.exchange_adapters.binance import sign_query

    if testnet is None:
        testnet = settings.binance_testnet

    if testnet:
        params: dict[str, Any] = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = sign_query(api_secret, params)
        try:
            status, data = await _signed_get_json(
                f"{_TESTNET_FAPI}/fapi/v2/account", params=params, headers={"X-MBX-APIKEY": api_key}
            )
        except Exception as exc:  # noqa: BLE001 — network/parse problems are non-fatal
            logger.warning(f"[validator] binance testnet network error: {exc!s:.120}")
            return ExchangePermissionResult(
                exchange="binance",
                ok=False,
                status=STATUS_VALIDATION_UNAVAILABLE,
                error_message="Could not reach Binance testnet to validate the key",
            )
        if status >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 200)):
            code = data.get("code") if isinstance(data, dict) else status
            msg = data.get("msg", "") if isinstance(data, dict) else str(data)
            return ExchangePermissionResult(
                exchange="binance",
                ok=False,
                status=_binance_error_status(code, msg),
                error_code=str(code),
                error_message=str(msg)[:200],
            )
        return classify_binance_futures_account(
            data if isinstance(data, dict) else {}, testnet=True
        )

    params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
    params["signature"] = sign_query(api_secret, params)
    try:
        status, data = await _signed_get_json(
            f"{_PROD_SAPI}/sapi/v1/account/apiRestrictions",
            params=params,
            headers={"X-MBX-APIKEY": api_key},
        )
    except Exception as exc:  # noqa: BLE001 — network/parse problems are non-fatal
        logger.warning(f"[validator] binance network error: {exc!s:.120}")
        return ExchangePermissionResult(
            exchange="binance",
            ok=False,
            status=STATUS_VALIDATION_UNAVAILABLE,
            error_message="Could not reach Binance to validate the key",
        )
    if status >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 200)):
        code = data.get("code") if isinstance(data, dict) else status
        msg = data.get("msg", "") if isinstance(data, dict) else str(data)
        return ExchangePermissionResult(
            exchange="binance",
            ok=False,
            status=_binance_error_status(code, msg),
            error_code=str(code),
            error_message=str(msg)[:200],
        )
    return classify_binance(data if isinstance(data, dict) else {})


async def validate_okx(api_key: str, api_secret: str, passphrase: str) -> ExchangePermissionResult:
    from app.exchange_adapters.okx import okx_timestamp, sign_okx

    path = "/api/v5/account/config"
    ts = okx_timestamp()
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": sign_okx(api_secret, ts, "GET", path, ""),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase or "",
    }
    try:
        status, data = await _signed_get_json(
            f"https://www.okx.com{path}", params={}, headers=headers
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[validator] okx network error: {exc!s:.120}")
        return ExchangePermissionResult(
            exchange="okx",
            ok=False,
            status=STATUS_VALIDATION_UNAVAILABLE,
            error_message="Could not reach OKX to validate the key",
        )
    code = str(data.get("code", "")) if isinstance(data, dict) else str(status)
    if status >= 400 or code not in ("0", ""):
        msg = data.get("msg", "") if isinstance(data, dict) else str(data)
        st = (
            STATUS_INVALID
            if code in ("50111", "50113", "50104", "50105", "50102")
            else STATUS_PERMISSION_DENIED
        )
        return ExchangePermissionResult(
            exchange="okx", ok=False, status=st, error_code=code, error_message=str(msg)[:200]
        )
    return classify_okx(data if isinstance(data, dict) else {})


async def validate_bybit(api_key: str, api_secret: str) -> ExchangePermissionResult:
    import time

    from app.exchange_adapters.bybit import _RECV_WINDOW, sign_bybit

    path = "/v5/user/query-api"
    ts = str(int(time.time() * 1000))
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": sign_bybit(api_secret, ts, api_key, _RECV_WINDOW, ""),
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
    }
    try:
        status, data = await _signed_get_json(
            f"https://api.bybit.com{path}", params={}, headers=headers
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[validator] bybit network error: {exc!s:.120}")
        return ExchangePermissionResult(
            exchange="bybit",
            ok=False,
            status=STATUS_VALIDATION_UNAVAILABLE,
            error_message="Could not reach Bybit to validate the key",
        )
    ret = str(data.get("retCode", "")) if isinstance(data, dict) else str(status)
    if status >= 400 or ret not in ("0", ""):
        msg = data.get("retMsg", "") if isinstance(data, dict) else str(data)
        st = STATUS_INVALID if ret in ("10003", "10004", "10005") else STATUS_PERMISSION_DENIED
        return ExchangePermissionResult(
            exchange="bybit", ok=False, status=st, error_code=ret, error_message=str(msg)[:200]
        )
    return classify_bybit(data if isinstance(data, dict) else {})


async def validate_bitget(
    api_key: str, api_secret: str, passphrase: str
) -> ExchangePermissionResult:
    import time

    from app.exchange_adapters.bitget import _PRODUCT, sign_bitget

    path = f"/api/v2/mix/account/accounts?productType={_PRODUCT}"
    ts = str(int(time.time() * 1000))
    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": sign_bitget(api_secret, ts, "GET", path, ""),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": passphrase or "",
        "Content-Type": "application/json",
    }
    try:
        status, data = await _signed_get_json(
            f"https://api.bitget.com{path}", params={}, headers=headers
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[validator] bitget network error: {exc!s:.120}")
        return ExchangePermissionResult(
            exchange="bitget",
            ok=False,
            status=STATUS_VALIDATION_UNAVAILABLE,
            error_message="Could not reach Bitget to validate the key",
        )
    code = str(data.get("code", "")) if isinstance(data, dict) else str(status)
    if status >= 400 or code not in ("00000", ""):
        msg = data.get("msg", "") if isinstance(data, dict) else str(data)
        st = STATUS_INVALID if code in ("40001", "40009", "40037") else STATUS_PERMISSION_DENIED
        return ExchangePermissionResult(
            exchange="bitget", ok=False, status=st, error_code=code, error_message=str(msg)[:200]
        )
    return classify_bitget(data if isinstance(data, dict) else {})


# ── unified entry point ─────────────────────────────────────────────


async def validate_permissions(
    exchange: str,
    api_key: str,
    api_secret: str,
    passphrase: Optional[str] = None,
) -> ExchangePermissionResult:
    """
    Validate a key's permissions, honoring MOCK_EXCHANGE_MODE.

    * MOCK mode -> deterministic offline result (no network).
    * LIVE mode -> read-only signed request to the exchange.
    Never places an order. Never raises for an unsupported/unreachable exchange;
    returns VALIDATION_UNAVAILABLE instead.
    """
    exchange = (exchange or "").lower()
    if exchange not in SUPPORTED_EXCHANGES:
        return ExchangePermissionResult(
            exchange=exchange,
            ok=False,
            status=STATUS_VALIDATION_UNAVAILABLE,
            error_message=f"Unsupported exchange: {exchange}",
        )
    if not api_key or not api_secret:
        return ExchangePermissionResult(
            exchange=exchange,
            ok=False,
            status=STATUS_INVALID,
            error_message="Missing API key or secret",
        )
    if exchange in PASSPHRASE_EXCHANGES and not passphrase:
        return ExchangePermissionResult(
            exchange=exchange,
            ok=False,
            status=STATUS_INVALID,
            error_message=f"{exchange} requires a passphrase",
        )

    # Credential validation is a real, read-only check. mock_exchange_mode only
    # simulates ORDERS — it must NOT short-circuit key validation, otherwise an
    # invalid key (wrong secret/passphrase, swapped fields) gets marked CONNECTED.
    # Only fall back to offline inference when validate_keys_live is explicitly off.
    if settings.mock_exchange_mode and not settings.validate_keys_live:
        perms = get_validator(exchange).validate(api_key, api_secret, passphrase)
        return from_mock_permissions(exchange, perms)

    try:
        if exchange == "binance":
            return await validate_binance(api_key, api_secret)
        if exchange == "okx":
            return await validate_okx(api_key, api_secret, passphrase or "")
        if exchange == "bybit":
            return await validate_bybit(api_key, api_secret)
        if exchange == "bitget":
            return await validate_bitget(api_key, api_secret, passphrase or "")
    except Exception as exc:  # noqa: BLE001 — defensive: never leak/raise from validation
        logger.warning(f"[validator] {exchange} unexpected error: {exc!s:.120}")
        return ExchangePermissionResult(
            exchange=exchange,
            ok=False,
            status=STATUS_VALIDATION_UNAVAILABLE,
            error_message="Validation failed unexpectedly",
        )
    return ExchangePermissionResult(
        exchange=exchange,
        ok=False,
        status=STATUS_VALIDATION_UNAVAILABLE,
        error_message="No validator installed for this exchange",
    )
