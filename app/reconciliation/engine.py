"""
Sprint 21B — execution reconciliation engine.

Detects drift between the local database and the real exchange:
  * positions present in one side but not the other,
  * size / entry / leverage / margin-mode / side mismatches,
  * TP/SL protection present in one side but missing in the other.

SAFETY: the engine is strictly READ-ONLY. It calls only get_positions /
get_open_orders on the adapter and writes ReconciliationIssue audit rows. It
NEVER opens, closes, or cancels an order. Auto-close is a separate, explicit
safety action (see app.recovery / emergency_close), never triggered from here.

The pure ``reconcile_symbol`` function takes already-fetched state so it is
fully unit-testable without a DB or network.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ExchangeAccount, LivePosition
from app.exchange_adapters import live_gate_open
from app.reconciliation.models import ReconciliationIssue
from app.utils.logger import logger

# ── severities ──────────────────────────────────────────────────────
SEV_INFO = "INFO"
SEV_WARNING = "WARNING"
SEV_CRITICAL = "CRITICAL"

# ── issue types ─────────────────────────────────────────────────────
DB_POSITION_MISSING_ON_EXCHANGE = "DB_POSITION_MISSING_ON_EXCHANGE"
EXCHANGE_POSITION_MISSING_IN_DB = "EXCHANGE_POSITION_MISSING_IN_DB"
TP_SL_MISSING_ON_EXCHANGE = "TP_SL_MISSING_ON_EXCHANGE"
TP_SL_MISSING_IN_DB = "TP_SL_MISSING_IN_DB"
SIZE_MISMATCH = "SIZE_MISMATCH"
ENTRY_MISMATCH = "ENTRY_MISMATCH"
LEVERAGE_MISMATCH = "LEVERAGE_MISMATCH"
MODE_MISMATCH = "MODE_MISMATCH"
SIDE_MISMATCH = "SIDE_MISMATCH"
STATUS_MISMATCH = "STATUS_MISMATCH"
UNKNOWN_ORDER = "UNKNOWN_ORDER"
ORPHAN_POSITION = "ORPHAN_POSITION"

ALL_ISSUE_TYPES = (
    DB_POSITION_MISSING_ON_EXCHANGE,
    EXCHANGE_POSITION_MISSING_IN_DB,
    TP_SL_MISSING_ON_EXCHANGE,
    TP_SL_MISSING_IN_DB,
    SIZE_MISMATCH,
    ENTRY_MISMATCH,
    LEVERAGE_MISMATCH,
    MODE_MISMATCH,
    SIDE_MISMATCH,
    STATUS_MISMATCH,
    UNKNOWN_ORDER,
    ORPHAN_POSITION,
)


@dataclass
class DriftIssue:
    issue_type: str
    severity: str
    symbol: str
    recommended_action: str
    db_state: dict[str, Any] = field(default_factory=dict)
    exchange_state: dict[str, Any] = field(default_factory=dict)


def _rel_diff(a: float, b: float) -> float:
    base = max(abs(a), abs(b), 1e-9)
    return abs(a - b) / base


# ════════════════════════════════════════════════════════════════════
#  Pure detection (no DB, no network — unit-tested)
# ════════════════════════════════════════════════════════════════════


def reconcile_symbol(
    symbol: str,
    db_pos: Optional[dict],
    ex_pos: Optional[dict],
    *,
    db_protection: Optional[dict] = None,  # {"has_tp": bool, "has_sl": bool}
    ex_protection: Optional[dict] = None,  # None == unknown (skip TP/SL checks)
    size_tol: float = 0.02,
    price_tol: float = 0.005,
) -> list[DriftIssue]:
    """
    Compare one symbol's DB position against the exchange position and return the
    drift issues found. ``db_pos`` / ``ex_pos`` are dicts (or None when absent)
    with keys: side, qty, entry_price, leverage, margin_type[, status].
    """
    issues: list[DriftIssue] = []

    if db_pos and not ex_pos:
        issues.append(
            DriftIssue(
                DB_POSITION_MISSING_ON_EXCHANGE,
                SEV_CRITICAL,
                symbol,
                "DB shows an OPEN position the exchange does not have. Mark it "
                "CLOSED_UNKNOWN after manual review; do not auto-manage.",
                db_state=db_pos,
                exchange_state={},
            )
        )
        return issues

    if ex_pos and not db_pos:
        issues.append(
            DriftIssue(
                EXCHANGE_POSITION_MISSING_IN_DB,
                SEV_CRITICAL,
                symbol,
                "Exchange holds a position with no DB record (orphan). Import as "
                "RECOVERED with requires_review=true; do not open anything new.",
                db_state={},
                exchange_state=ex_pos,
            )
        )
        return issues

    if not db_pos and not ex_pos:
        return issues

    # Both present — compare fields.
    if str(db_pos.get("side", "")).upper() != str(ex_pos.get("side", "")).upper():
        issues.append(
            DriftIssue(
                SIDE_MISMATCH,
                SEV_CRITICAL,
                symbol,
                "Position side differs between DB and exchange. Reconcile before any "
                "further action; risk of opening an opposite position.",
                db_state=db_pos,
                exchange_state=ex_pos,
            )
        )

    dq, eq = float(db_pos.get("qty", 0)), float(ex_pos.get("qty", 0))
    if _rel_diff(dq, eq) > size_tol:
        sev = SEV_CRITICAL if _rel_diff(dq, eq) > 0.10 else SEV_WARNING
        issues.append(
            DriftIssue(
                SIZE_MISMATCH,
                sev,
                symbol,
                "Position size differs. Re-sync DB quantity from the exchange.",
                db_state={"qty": dq},
                exchange_state={"qty": eq},
            )
        )

    de, ee = float(db_pos.get("entry_price", 0)), float(ex_pos.get("entry_price", 0))
    if de > 0 and ee > 0 and _rel_diff(de, ee) > price_tol:
        issues.append(
            DriftIssue(
                ENTRY_MISMATCH,
                SEV_WARNING,
                symbol,
                "Entry price differs (likely partial fills/averaging). Re-sync entry.",
                db_state={"entry_price": de},
                exchange_state={"entry_price": ee},
            )
        )

    dl, el = int(db_pos.get("leverage", 0) or 0), int(ex_pos.get("leverage", 0) or 0)
    if el and dl != el:
        issues.append(
            DriftIssue(
                LEVERAGE_MISMATCH,
                SEV_WARNING,
                symbol,
                "Leverage differs. Re-sync leverage from the exchange.",
                db_state={"leverage": dl},
                exchange_state={"leverage": el},
            )
        )

    dm = str(db_pos.get("margin_type", "")).lower()
    em = str(ex_pos.get("margin_type", "")).lower()
    if dm and em and dm != em:
        issues.append(
            DriftIssue(
                MODE_MISMATCH,
                SEV_WARNING,
                symbol,
                "Margin mode differs (isolated vs cross). Re-sync margin mode.",
                db_state={"margin_type": dm},
                exchange_state={"margin_type": em},
            )
        )

    # TP/SL protection — only when exchange protection is known.
    if ex_protection is not None:
        dbp = db_protection or {}
        db_protected = bool(dbp.get("has_tp") or dbp.get("has_sl"))
        ex_protected = bool(ex_protection.get("has_tp") or ex_protection.get("has_sl"))
        if db_protected and not ex_protected:
            issues.append(
                DriftIssue(
                    TP_SL_MISSING_ON_EXCHANGE,
                    SEV_CRITICAL,
                    symbol,
                    "DB expects TP/SL but the exchange has none — position is "
                    "UNPROTECTED. Mark UNSAFE and retry TP/SL placement.",
                    db_state=dbp,
                    exchange_state=ex_protection,
                )
            )
        elif ex_protected and not db_protected:
            issues.append(
                DriftIssue(
                    TP_SL_MISSING_IN_DB,
                    SEV_INFO,
                    symbol,
                    "Exchange has TP/SL orders not tracked in the DB. Attach order " "references.",
                    db_state=dbp,
                    exchange_state=ex_protection,
                )
            )

    return issues


# ════════════════════════════════════════════════════════════════════
#  DB-backed orchestration (read-only)
# ════════════════════════════════════════════════════════════════════


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pos_to_dict(p: LivePosition) -> dict:
    return {
        "side": p.side,
        "qty": p.quantity,
        "entry_price": p.entry_price,
        "leverage": p.leverage,
        "margin_type": p.margin_type,
        "status": p.status,
    }


def _ex_pos_to_dict(pi) -> dict:
    return {
        "side": pi.side,
        "qty": pi.qty,
        "entry_price": pi.entry_price,
        "leverage": pi.leverage,
        "margin_type": pi.margin_type,
    }


async def _existing_unresolved(
    db: AsyncSession, user_id: int, exchange: str, symbol: str, issue_type: str
) -> bool:
    res = await db.execute(
        select(ReconciliationIssue.id)
        .where(
            ReconciliationIssue.user_id == user_id,
            ReconciliationIssue.exchange == exchange,
            ReconciliationIssue.symbol == symbol,
            ReconciliationIssue.issue_type == issue_type,
            ReconciliationIssue.resolved == False,  # noqa: E712
        )
        .limit(1)
    )
    return res.first() is not None


async def reconcile_exchange_account(
    db: AsyncSession,
    *,
    user_id: int,
    exchange: str,
    persist: bool = True,
) -> dict:
    """Reconcile one user's positions on one exchange. Read-only."""
    from app.exchange_adapters import resolve_adapter
    from app.exchange_vault import service as vault

    mode = "LIVE" if live_gate_open() else "MOCK"
    # DB open positions for this exchange.
    rows = (
        (
            await db.execute(
                select(LivePosition).where(
                    LivePosition.user_id == user_id,
                    LivePosition.exchange == exchange,
                    LivePosition.status == "OPEN",
                )
            )
        )
        .scalars()
        .all()
    )
    db_by_symbol = {p.symbol: p for p in rows}

    # Exchange positions + open orders (read-only).
    ex_positions: list = []
    ex_orders: list = []
    try:
        creds = await vault.get_decrypted_credentials(db, user_id, exchange)
        adapter = resolve_adapter(
            exchange,
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            passphrase=creds.get("passphrase"),
        )
        try:
            ex_positions = await adapter.get_positions()
            ex_orders = await adapter.get_open_orders()
        finally:
            await adapter.close()
    except Exception as exc:  # noqa: BLE001 — unreachable exchange is itself reportable, not fatal
        logger.warning(f"[reconcile] user={user_id} {exchange} read failed: {exc!s:.120}")

    ex_by_symbol = {pi.symbol: pi for pi in ex_positions}
    # Map open TP/SL orders to protection flags per symbol.
    ex_prot: dict[str, dict] = {}
    for o in ex_orders:
        d = ex_prot.setdefault(o.symbol, {"has_tp": False, "has_sl": False})
        t = (o.type or "").upper()
        if "TAKE_PROFIT" in t:
            d["has_tp"] = True
        if "STOP" in t:
            d["has_sl"] = True

    drift: list[DriftIssue] = []
    for symbol in set(db_by_symbol) | set(ex_by_symbol):
        db_pos = _pos_to_dict(db_by_symbol[symbol]) if symbol in db_by_symbol else None
        ex_pos = _ex_pos_to_dict(ex_by_symbol[symbol]) if symbol in ex_by_symbol else None
        # Only assert TP/SL drift when we actually fetched orders for a real
        # adapter; the default adapter returns [] (unknown) -> skip those checks.
        ex_protection = (
            ex_prot.get(symbol, {"has_tp": False, "has_sl": False}) if ex_orders else None
        )
        drift.extend(reconcile_symbol(symbol, db_pos, ex_pos, ex_protection=ex_protection))

    created = 0
    if persist:
        for di in drift:
            if await _existing_unresolved(db, user_id, exchange, di.symbol, di.issue_type):
                continue
            db.add(
                ReconciliationIssue(
                    user_id=user_id,
                    exchange=exchange,
                    symbol=di.symbol,
                    mode=mode,
                    severity=di.severity,
                    issue_type=di.issue_type,
                    db_state=json.dumps(di.db_state),
                    exchange_state=json.dumps(di.exchange_state),
                    recommended_action=di.recommended_action,
                )
            )
            created += 1
        # Stamp reconciliation time on the DB positions we examined.
        for p in rows:
            p.last_reconciled_at = _now()
        await db.flush()

    return {
        "user_id": user_id,
        "exchange": exchange,
        "mode": mode,
        "db_positions": len(db_by_symbol),
        "exchange_positions": len(ex_by_symbol),
        "issues_found": len(drift),
        "issues_created": created,
        "issues": [asdict(d) for d in drift],
    }


async def reconcile_user(db: AsyncSession, *, user_id: int, persist: bool = True) -> dict:
    """Reconcile every CONNECTED exchange for one user."""
    accounts = (
        (
            await db.execute(
                select(ExchangeAccount).where(
                    ExchangeAccount.user_id == user_id, ExchangeAccount.status == "CONNECTED"
                )
            )
        )
        .scalars()
        .all()
    )
    exchanges = sorted({a.exchange for a in accounts})
    results = [
        await reconcile_exchange_account(db, user_id=user_id, exchange=ex, persist=persist)
        for ex in exchanges
    ]
    return {
        "user_id": user_id,
        "exchanges": exchanges,
        "issues_found": sum(r["issues_found"] for r in results),
        "issues_created": sum(r["issues_created"] for r in results),
        "results": results,
    }


async def reconcile_all_active_users(db: AsyncSession, *, persist: bool = True) -> dict:
    """Reconcile all users that have at least one CONNECTED exchange account."""
    user_ids = sorted(
        {
            uid
            for (uid,) in (
                await db.execute(
                    select(ExchangeAccount.user_id).where(ExchangeAccount.status == "CONNECTED")
                )
            ).all()
        }
    )
    per_user = [await reconcile_user(db, user_id=uid, persist=persist) for uid in user_ids]
    return {
        "users": len(user_ids),
        "issues_found": sum(u["issues_found"] for u in per_user),
        "issues_created": sum(u["issues_created"] for u in per_user),
        "per_user": per_user,
    }
