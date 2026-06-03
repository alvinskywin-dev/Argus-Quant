"""
Sprint 21C — recovery status aggregates (read-only, no PII).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LivePosition
from app.recovery import tp_sl


async def recovery_status(db: AsyncSession) -> dict:
    async def _count(*where) -> int:
        return int(
            (
                await db.execute(select(func.count()).select_from(LivePosition).where(*where))
            ).scalar_one()
            or 0
        )

    unsafe = await _count(LivePosition.tp_sl_status == tp_sl.UNSAFE)
    recovered = await _count(LivePosition.status == "RECOVERED")
    closed_unknown = await _count(LivePosition.status == "CLOSED_UNKNOWN")
    requires_review = await _count(LivePosition.requires_review == True)  # noqa: E712
    open_positions = await _count(LivePosition.status == "OPEN")
    last_reconciled = (
        await db.execute(select(func.max(LivePosition.last_reconciled_at)))
    ).scalar_one_or_none()
    return {
        "open_positions": open_positions,
        "recovered_positions": recovered,
        "closed_unknown": closed_unknown,
        "unsafe_positions": unsafe,
        "requires_review": requires_review,
        "last_recovery_at": last_reconciled.isoformat() if last_reconciled else None,
    }
