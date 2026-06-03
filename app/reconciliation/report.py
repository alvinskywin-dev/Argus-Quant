"""
Sprint 21B — reconciliation reporting helpers (read-only aggregates).
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.reconciliation.models import ReconciliationIssue


def _issue_dict(i: ReconciliationIssue) -> dict:
    def _load(s):
        try:
            return json.loads(s) if s else {}
        except Exception:  # noqa: BLE001
            return {}

    return {
        "id": i.id,
        "user_id": i.user_id,
        "exchange": i.exchange,
        "symbol": i.symbol,
        "mode": i.mode,
        "severity": i.severity,
        "issue_type": i.issue_type,
        "db_state": _load(i.db_state),
        "exchange_state": _load(i.exchange_state),
        "recommended_action": i.recommended_action,
        "resolved": i.resolved,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
    }


async def summary(db: AsyncSession, *, user_id: Optional[int] = None) -> dict:
    """Aggregate counts by severity + unresolved total. No PII."""
    q = select(ReconciliationIssue.severity, ReconciliationIssue.resolved, func.count()).group_by(
        ReconciliationIssue.severity, ReconciliationIssue.resolved
    )
    if user_id is not None:
        q = q.where(ReconciliationIssue.user_id == user_id)
    by_sev: dict[str, int] = {}
    unresolved = 0
    total = 0
    for sev, resolved, n in (await db.execute(q)).all():
        total += n
        by_sev[sev] = by_sev.get(sev, 0) + n
        if not resolved:
            unresolved += n
    last = (await db.execute(select(func.max(ReconciliationIssue.created_at)))).scalar_one_or_none()
    return {
        "total_issues": total,
        "unresolved": unresolved,
        "by_severity": by_sev,
        "last_issue_at": last.isoformat() if last else None,
    }


async def list_issues(
    db: AsyncSession,
    *,
    user_id: Optional[int] = None,
    resolved: Optional[bool] = None,
    limit: int = 200,
) -> list[dict]:
    q = select(ReconciliationIssue)
    if user_id is not None:
        q = q.where(ReconciliationIssue.user_id == user_id)
    if resolved is not None:
        q = q.where(ReconciliationIssue.resolved == resolved)
    q = q.order_by(ReconciliationIssue.created_at.desc()).limit(limit)
    return [_issue_dict(i) for i in (await db.execute(q)).scalars().all()]
