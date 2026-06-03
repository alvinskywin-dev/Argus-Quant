"""
Sprint 21C — position recovery engine.

Rebuilds local trading state after a restart, crash, deploy, or exchange
disconnect, and re-secures positions whose TP/SL orders went missing.

SAFETY GUARANTEES
  * Recovery NEVER opens a new position and never increases exposure.
  * The only orders it may place are PROTECTIVE TP/SL (reduce-only), and only
    when POSITION_RECOVERY_ENABLED is true and the position has stored targets.
  * Orphan exchange positions are imported as RECOVERED + requires_review and
    are NOT auto-managed until a human reviews them.
  * Every action is audited and, for drift, a ReconciliationIssue is recorded.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import ExchangeAccount, LiveAuditLog, LivePosition
from app.database.session import SessionLocal
from app.exchange_adapters import live_gate_open, resolve_adapter
from app.exchange_adapters.base import AdapterError, to_side
from app.exchange_vault import service as vault
from app.reconciliation import engine as recon
from app.reconciliation.models import ReconciliationIssue
from app.recovery import tp_sl
from app.utils.logger import logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mode() -> str:
    return "LIVE" if live_gate_open() else "MOCK"


async def _alert_admin(
    user_id: Optional[int], exchange: str, symbol: str, detail: str, result: str = "OK"
) -> None:
    """Record a recovery action / alert in its own session (survives rollback)."""
    try:
        async with SessionLocal() as adb:
            adb.add(
                LiveAuditLog(
                    user_id=user_id,
                    exchange=exchange,
                    symbol=symbol or "",
                    action="RECOVERY",
                    result=result,
                    mode=_mode(),
                    detail=(detail or "")[:512],
                )
            )
            await adb.commit()
    except Exception as exc:  # noqa: BLE001 — alerting must never break recovery
        logger.warning(f"[recovery] alert write failed: {exc}")
    logger.info(f"[recovery] user={user_id} {exchange} {symbol}: {detail}")


async def _record_issue(
    db: AsyncSession,
    *,
    user_id: int,
    exchange: str,
    symbol: str,
    issue_type: str,
    severity: str,
    recommended: str,
    db_state: dict,
    exchange_state: dict,
) -> None:
    if await recon._existing_unresolved(db, user_id, exchange, symbol, issue_type):
        return
    db.add(
        ReconciliationIssue(
            user_id=user_id,
            exchange=exchange,
            symbol=symbol,
            mode=_mode(),
            severity=severity,
            issue_type=issue_type,
            db_state=json.dumps(db_state),
            exchange_state=json.dumps(exchange_state),
            recommended_action=recommended,
        )
    )


# ── mark unsafe ─────────────────────────────────────────────────────


async def mark_position_unsafe(
    db: AsyncSession, position_id: int, reason: str
) -> Optional[LivePosition]:
    pos = await db.get(LivePosition, position_id)
    if pos is None:
        return None
    pos.tp_sl_status = tp_sl.UNSAFE
    pos.unsafe_reason = (reason or "")[:256]
    pos.requires_review = True
    await db.flush()
    await _alert_admin(
        pos.user_id, pos.exchange, pos.symbol, f"POSITION MARKED UNSAFE: {reason}", result="FAIL"
    )
    return pos


# ── TP/SL sync for one position ─────────────────────────────────────


async def _open_protection(adapter, symbol: str) -> dict:
    """Read working orders for a symbol -> {has_tp, has_sl}."""
    try:
        orders = await adapter.get_open_orders(symbol)
    except AdapterError:
        return {"has_tp": None, "has_sl": None}  # unknown
    has_tp = any("TAKE_PROFIT" in (o.type or "").upper() for o in orders)
    has_sl = any("STOP" in (o.type or "").upper() for o in orders)
    return {"has_tp": has_tp, "has_sl": has_sl}


async def sync_tp_sl_for_position(
    db: AsyncSession, position_id: int, *, attempt_replace: bool = True
) -> dict:
    """
    Verify (and, if enabled+possible, restore) the TP/SL protection of one open
    position. Returns a summary dict. Only places PROTECTIVE reduce-only orders.
    """
    pos = await db.get(LivePosition, position_id)
    if pos is None:
        return {"error": "not found"}
    if pos.status not in ("OPEN", "RECOVERED"):
        return {"position_id": position_id, "tp_sl_status": pos.tp_sl_status, "skipped": "not open"}

    expected_tp = pos.take_profit is not None and pos.take_profit > 0
    expected_sl = pos.stop_loss is not None and pos.stop_loss > 0

    try:
        creds = await vault.get_decrypted_credentials(db, pos.user_id, pos.exchange)
        adapter = resolve_adapter(
            pos.exchange,
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            passphrase=creds.get("passphrase"),
        )
    except vault.VaultError:
        pos.tp_sl_status = tp_sl.UNKNOWN
        await db.flush()
        return {
            "position_id": position_id,
            "tp_sl_status": tp_sl.UNKNOWN,
            "reason": "no credentials",
        }

    try:
        prot = await _open_protection(adapter, pos.symbol)
        if prot["has_tp"] is None:  # couldn't read orders
            pos.tp_sl_status = tp_sl.UNKNOWN
            await db.flush()
            return {
                "position_id": position_id,
                "tp_sl_status": tp_sl.UNKNOWN,
                "reason": "orders unreadable",
            }

        status = tp_sl.compute_tp_sl_status(
            bool(prot["has_tp"]),
            bool(prot["has_sl"]),
            expected_tp=expected_tp,
            expected_sl=expected_sl,
        )

        retries = 0
        if status != tp_sl.SYNCED and attempt_replace and settings.position_recovery_enabled:
            # Re-place ONLY the missing protective legs, up to the retry budget.
            for _ in range(max(1, settings.tp_sl_retry_max)):
                retries += 1
                try:
                    await adapter.set_tp_sl(
                        symbol=pos.symbol,
                        side=to_side(pos.side),
                        qty=pos.quantity,
                        take_profit=(
                            pos.take_profit if (expected_tp and not prot["has_tp"]) else None
                        ),
                        stop_loss=pos.stop_loss if (expected_sl and not prot["has_sl"]) else None,
                    )
                    prot = await _open_protection(adapter, pos.symbol)
                    status = tp_sl.compute_tp_sl_status(
                        bool(prot["has_tp"]),
                        bool(prot["has_sl"]),
                        expected_tp=expected_tp,
                        expected_sl=expected_sl,
                    )
                    if status == tp_sl.SYNCED:
                        break
                except AdapterError as exc:
                    await _alert_admin(
                        pos.user_id,
                        pos.exchange,
                        pos.symbol,
                        f"TP/SL retry failed: {exc}",
                        result="FAIL",
                    )

        if status == tp_sl.SYNCED:
            pos.tp_sl_status = tp_sl.SYNCED
            pos.unsafe_reason = None
            await db.flush()
            return {"position_id": position_id, "tp_sl_status": status, "retries": retries}

        # Still unprotected — quarantine + flag for review / emergency close.
        pos.tp_sl_status = tp_sl.UNSAFE if tp_sl.is_unsafe_status(status) else status
        pos.unsafe_reason = f"TP/SL not synced after {retries} retries ({status})"
        pos.requires_review = True
        await _record_issue(
            db,
            user_id=pos.user_id,
            exchange=pos.exchange,
            symbol=pos.symbol,
            issue_type=recon.TP_SL_MISSING_ON_EXCHANGE,
            severity=recon.SEV_CRITICAL,
            recommended="Retry TP/SL or emergency-close (reduce-only) after review.",
            db_state={"take_profit": pos.take_profit, "stop_loss": pos.stop_loss},
            exchange_state=prot,
        )
        await db.flush()
        await _alert_admin(
            pos.user_id,
            pos.exchange,
            pos.symbol,
            f"UNPROTECTED position: {pos.unsafe_reason}",
            result="FAIL",
        )
        return {
            "position_id": position_id,
            "tp_sl_status": pos.tp_sl_status,
            "retries": retries,
            "unsafe": True,
        }
    finally:
        await adapter.close()


async def recover_tp_sl_state(db: AsyncSession, position_id: int) -> dict:
    """Public alias: verify/restore TP/SL state for one position."""
    return await sync_tp_sl_for_position(db, position_id)


# ── full per-user recovery ──────────────────────────────────────────


async def recover_user_positions(db: AsyncSession, *, user_id: int) -> dict:
    """
    Rebuild state for one user across all CONNECTED exchanges. Read exchange
    positions, reconcile against the DB, import orphans as RECOVERED, mark
    vanished DB positions CLOSED_UNKNOWN, and re-secure TP/SL. Opens nothing.
    """
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

    recovered = closed_unknown = unsafe = reviewed = synced = 0

    for acc in accounts:
        exchange = acc.exchange
        db_rows = (
            (
                await db.execute(
                    select(LivePosition).where(
                        LivePosition.user_id == user_id,
                        LivePosition.exchange == exchange,
                        LivePosition.status.in_(["OPEN", "RECOVERED"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        db_by_symbol = {p.symbol: p for p in db_rows}

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
            finally:
                await adapter.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[recovery] user={user_id} {exchange} positions read failed: {exc!s:.120}"
            )
            continue

        ex_by_symbol = {pi.symbol: pi for pi in ex_positions}

        # 1) Orphan exchange positions -> import as RECOVERED, requires_review.
        for symbol, pi in ex_by_symbol.items():
            if symbol in db_by_symbol:
                continue
            db.add(
                LivePosition(
                    user_id=user_id,
                    exchange=exchange,
                    symbol=symbol,
                    side=pi.side,
                    quantity=pi.qty,
                    entry_price=pi.entry_price,
                    leverage=pi.leverage,
                    margin_type=pi.margin_type,
                    status="RECOVERED",
                    mode=_mode(),
                    tp_sl_status=tp_sl.UNKNOWN,
                    requires_review=True,
                    recovered_at=_now(),
                    last_reconciled_at=_now(),
                )
            )
            recovered += 1
            reviewed += 1
            await _record_issue(
                db,
                user_id=user_id,
                exchange=exchange,
                symbol=symbol,
                issue_type=recon.EXCHANGE_POSITION_MISSING_IN_DB,
                severity=recon.SEV_CRITICAL,
                recommended="Imported as RECOVERED; review before auto-managing.",
                db_state={},
                exchange_state=recon._ex_pos_to_dict(pi),
            )
            await _alert_admin(
                user_id, exchange, symbol, "Orphan exchange position imported as RECOVERED"
            )

        # 2) DB positions gone from the exchange -> CLOSED_UNKNOWN.
        for symbol, p in db_by_symbol.items():
            if symbol in ex_by_symbol:
                continue
            p.status = "CLOSED_UNKNOWN"
            p.closed_at = _now()
            p.last_reconciled_at = _now()
            p.requires_review = True
            closed_unknown += 1
            reviewed += 1
            await _record_issue(
                db,
                user_id=user_id,
                exchange=exchange,
                symbol=symbol,
                issue_type=recon.DB_POSITION_MISSING_ON_EXCHANGE,
                severity=recon.SEV_CRITICAL,
                recommended="Position not on exchange; marked CLOSED_UNKNOWN for review.",
                db_state=recon._pos_to_dict(p),
                exchange_state={},
            )
            await _alert_admin(
                user_id,
                exchange,
                symbol,
                "DB position missing on exchange -> CLOSED_UNKNOWN",
                result="FAIL",
            )

        await db.flush()

        # 3) Matched positions -> re-secure TP/SL.
        for symbol, p in db_by_symbol.items():
            if symbol not in ex_by_symbol:
                continue
            p.last_reconciled_at = _now()
            res = await sync_tp_sl_for_position(db, p.id)
            if res.get("unsafe"):
                unsafe += 1
            elif res.get("tp_sl_status") == tp_sl.SYNCED:
                synced += 1

    return {
        "user_id": user_id,
        "recovered": recovered,
        "closed_unknown": closed_unknown,
        "tp_sl_synced": synced,
        "unsafe": unsafe,
        "requires_review": reviewed,
    }


async def recover_all_positions(db: AsyncSession) -> dict:
    """Recover every user that has a CONNECTED exchange account."""
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
    per_user = [await recover_user_positions(db, user_id=uid) for uid in user_ids]
    return {
        "users": len(user_ids),
        "recovered": sum(u["recovered"] for u in per_user),
        "closed_unknown": sum(u["closed_unknown"] for u in per_user),
        "unsafe": sum(u["unsafe"] for u in per_user),
        "per_user": per_user,
    }
