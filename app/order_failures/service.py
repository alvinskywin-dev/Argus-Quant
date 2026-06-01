"""
Sprint 21D — order failure / retry orchestration (DB layer).

Records failures, applies the pure RetryPolicy decision, and exposes a circuit
breaker. This layer NEVER re-sends an order on its own — it persists the
decision (whether/when a retry is allowed, or whether the order must be
reconciled first). The execution layer reads the decision and acts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.order_failures import policy
from app.order_failures.models import OrderFailure
from app.utils.logger import logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def record_failure(
    db: AsyncSession, *, user_id: int, exchange: str, symbol: str, side: str,
    order_type: str = "MARKET", quantity: float = 0.0, price: float = 0.0,
    reduce_only: bool = False, error_message: str = "", error_code: str | int | None = None,
    is_tp_sl: bool = False, idempotency_key: Optional[str] = None,
    mode: str = "MOCK", recommended_delay: float | None = None,
) -> tuple[OrderFailure, policy.RetryDecision]:
    """Classify + persist a failed order attempt and compute its retry decision."""
    error_class = policy.classify_error(error_message, code=error_code, is_tp_sl=is_tp_sl)
    decision = policy.decide_retry(error_class, 0, max_retries=5, recommended_delay=recommended_delay)
    row = OrderFailure(
        idempotency_key=idempotency_key, user_id=user_id, exchange=exchange.lower(),
        symbol=symbol.upper(), order_type=order_type, side=side.upper(), quantity=quantity,
        price=price, reduce_only=reduce_only, mode=mode, error_class=error_class,
        error_code=(str(error_code) if error_code is not None else None),
        error_message=(error_message or "")[:512], retry_count=0,
        final_state=decision.final_state,
        next_retry_at=(_now() + timedelta(seconds=decision.delay_sec)) if decision.should_retry else None)
    db.add(row)
    await db.flush()
    logger.info(f"[order-fail] user={user_id} {exchange} {symbol} class={error_class} "
                f"state={decision.final_state} retry={decision.should_retry}")
    return row, decision


async def note_retry(db: AsyncSession, failure_id: int) -> dict:
    """
    Consume one retry from the budget and recompute the decision. Does NOT place
    an order — returns the fresh decision for the execution layer to act on.
    """
    row = await db.get(OrderFailure, failure_id)
    if row is None:
        return {"error": "not found"}
    if row.final_state in (policy.RESOLVED, policy.FAILED):
        return {"id": failure_id, "final_state": row.final_state, "note": "terminal"}
    row.retry_count += 1
    decision = policy.decide_retry(row.error_class, row.retry_count, max_retries=5)
    row.final_state = decision.final_state
    row.next_retry_at = (_now() + timedelta(seconds=decision.delay_sec)) if decision.should_retry else None
    await db.flush()
    return {
        "id": failure_id, "retry_count": row.retry_count, "final_state": row.final_state,
        "should_retry": decision.should_retry, "delay_sec": decision.delay_sec,
        "needs_reconcile": decision.needs_reconcile, "reason": decision.reason,
    }


async def mark_resolved(db: AsyncSession, failure_id: int) -> dict:
    row = await db.get(OrderFailure, failure_id)
    if row is None:
        return {"error": "not found"}
    row.final_state = policy.RESOLVED
    row.next_retry_at = None
    await db.flush()
    return {"id": failure_id, "final_state": row.final_state}


async def recent_failure_count(db: AsyncSession, user_id: int, window_sec: int) -> int:
    since = _now() - timedelta(seconds=window_sec)
    return int((await db.execute(
        select(func.count()).select_from(OrderFailure).where(
            OrderFailure.user_id == user_id,
            OrderFailure.created_at >= since))).scalar_one() or 0)


async def circuit_breaker_tripped(db: AsyncSession, user_id: int) -> bool:
    """
    True when the user exceeded the failure threshold inside the window. When
    tripped, the caller should disable that user's live auto-trading.
    """
    threshold = settings.order_failure_breaker_threshold
    window = settings.order_failure_breaker_window_sec
    if threshold <= 0:
        return False
    count = await recent_failure_count(db, user_id, window)
    tripped = count >= threshold
    if tripped:
        logger.warning(f"[order-fail] circuit breaker TRIPPED user={user_id} "
                       f"({count} failures / {window}s)")
    return tripped


async def has_active_duplicate(db: AsyncSession, idempotency_key: str) -> bool:
    """True if a non-terminal failure already exists for this idempotency key."""
    if not idempotency_key:
        return False
    res = await db.execute(
        select(OrderFailure.id).where(
            OrderFailure.idempotency_key == idempotency_key,
            OrderFailure.final_state.notin_([policy.RESOLVED, policy.FAILED])).limit(1))
    return res.first() is not None


def _row_dict(r: OrderFailure) -> dict:
    return {
        "id": r.id, "idempotency_key": r.idempotency_key, "user_id": r.user_id,
        "exchange": r.exchange, "symbol": r.symbol, "order_type": r.order_type,
        "side": r.side, "quantity": r.quantity, "price": r.price, "reduce_only": r.reduce_only,
        "mode": r.mode, "error_class": r.error_class, "error_code": r.error_code,
        "error_message": r.error_message, "retry_count": r.retry_count,
        "final_state": r.final_state,
        "next_retry_at": r.next_retry_at.isoformat() if r.next_retry_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


async def list_failures(db: AsyncSession, *, user_id: Optional[int] = None,
                        final_state: Optional[str] = None, limit: int = 200) -> list[dict]:
    q = select(OrderFailure)
    if user_id is not None:
        q = q.where(OrderFailure.user_id == user_id)
    if final_state:
        q = q.where(OrderFailure.final_state == final_state)
    q = q.order_by(OrderFailure.created_at.desc()).limit(limit)
    return [_row_dict(r) for r in (await db.execute(q)).scalars().all()]


async def get_failure(db: AsyncSession, failure_id: int) -> Optional[dict]:
    r = await db.get(OrderFailure, failure_id)
    return _row_dict(r) if r else None


async def summary(db: AsyncSession) -> dict:
    """Aggregate by final_state + error_class (no PII)."""
    by_state: dict[str, int] = {}
    for state, n in (await db.execute(
            select(OrderFailure.final_state, func.count()).group_by(
                OrderFailure.final_state))).all():
        by_state[state] = n
    by_class: dict[str, int] = {}
    for cls, n in (await db.execute(
            select(OrderFailure.error_class, func.count()).group_by(
                OrderFailure.error_class))).all():
        by_class[cls] = n
    return {"total": sum(by_state.values()), "by_state": by_state, "by_error_class": by_class}
