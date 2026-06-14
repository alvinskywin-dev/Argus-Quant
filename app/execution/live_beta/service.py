"""
Multi-user Live Beta — business logic.

Two responsibilities:
  1. Membership lifecycle: request access (invite code + risk agreement),
     admin approve / reject / suspend, capped by LIVE_BETA_MAX_USERS.
  2. `beta_gate`: the reusable pre-trade check the live execution path calls to
     decide whether a user may place a live order of a given size — enforcing
     per-user capital + position limits, the exchange allowlist, the per-symbol
     exposure limit, and the global exposure cap.

It never places or modifies orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import LivePosition
from app.execution.live_beta.models import APPROVED, PENDING, SUSPENDED, LiveBetaMember
from app.utils.logger import logger


class LiveBetaError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def beta_enabled() -> bool:
    return bool(settings.live_beta_enabled)


def _allowed_exchanges() -> list[str]:
    return [e.strip().lower() for e in settings.live_beta_allowed_exchanges.split(",") if e.strip()]


# ── lookups ───────────────────────────────────────────────────────


async def get_member(db: AsyncSession, user_id: int) -> Optional[LiveBetaMember]:
    res = await db.execute(select(LiveBetaMember).where(LiveBetaMember.user_id == user_id))
    return res.scalar_one_or_none()


async def list_members(db: AsyncSession) -> list[LiveBetaMember]:
    res = await db.execute(select(LiveBetaMember).order_by(LiveBetaMember.created_at.desc()))
    return list(res.scalars().all())


async def _member_count(db: AsyncSession) -> int:
    res = await db.execute(select(func.count(LiveBetaMember.id)))
    return int(res.scalar() or 0)


# ── lifecycle ─────────────────────────────────────────────────────


async def request_access(
    db: AsyncSession, *, user_id: int, invite_code: str, accept_risk: bool
) -> LiveBetaMember:
    if not beta_enabled():
        raise LiveBetaError(403, "Live beta is not open.")
    if not accept_risk:
        raise LiveBetaError(400, "You must accept the live-trading risk agreement.")
    if settings.live_beta_invite_code and invite_code != settings.live_beta_invite_code:
        raise LiveBetaError(403, "Invalid invite code.")

    existing = await get_member(db, user_id)
    if existing is not None:
        # Idempotent: re-accepting just refreshes the risk timestamp.
        existing.risk_agreement_accepted_at = _now()
        return existing

    if await _member_count(db) >= settings.live_beta_max_users:
        raise LiveBetaError(409, "Live beta is full.")

    auto_approve = not settings.live_beta_require_admin_approval
    member = LiveBetaMember(
        user_id=user_id,
        status=APPROVED if auto_approve else PENDING,
        max_notional=settings.live_beta_default_user_max_notional,
        max_positions=settings.live_beta_default_max_positions,
        allowed_exchanges=",".join(_allowed_exchanges()),
        invite_code_used=(invite_code or None),
        risk_agreement_accepted_at=_now(),
        approved_at=_now() if auto_approve else None,
    )
    db.add(member)
    await db.flush()
    logger.info(f"[beta] access requested user={user_id} status={member.status}")
    return member


async def approve(db: AsyncSession, *, admin_id: int, user_id: int) -> LiveBetaMember:
    member = await _require_member(db, user_id)
    member.status = APPROVED
    member.approved_by = admin_id
    member.approved_at = _now()
    member.suspended_reason = None
    logger.warning(f"[beta] APPROVED user={user_id} by admin={admin_id}")
    return member


async def reject(
    db: AsyncSession, *, admin_id: int, user_id: int, reason: str = ""
) -> LiveBetaMember:
    member = await _require_member(db, user_id)
    member.status = "REJECTED"
    member.suspended_reason = (reason or "")[:256] or None
    logger.warning(f"[beta] REJECTED user={user_id} by admin={admin_id}")
    return member


async def suspend(
    db: AsyncSession, *, admin_id: int, user_id: int, reason: str = ""
) -> LiveBetaMember:
    member = await _require_member(db, user_id)
    member.status = SUSPENDED
    member.suspended_reason = (reason or "")[:256] or None
    logger.warning(f"[beta] SUSPENDED user={user_id} by admin={admin_id}")
    return member


async def _require_member(db: AsyncSession, user_id: int) -> LiveBetaMember:
    member = await get_member(db, user_id)
    if member is None:
        raise LiveBetaError(404, "Beta member not found.")
    return member


# ── exposure helpers ──────────────────────────────────────────────


async def _user_open_notional(db: AsyncSession, user_id: int) -> float:
    res = await db.execute(
        select(
            func.coalesce(func.sum(LivePosition.quantity * LivePosition.entry_price), 0.0)
        ).where(LivePosition.user_id == user_id, LivePosition.status == "OPEN")
    )
    return float(res.scalar() or 0.0)


async def _user_open_positions(db: AsyncSession, user_id: int) -> int:
    res = await db.execute(
        select(func.count(LivePosition.id)).where(
            LivePosition.user_id == user_id, LivePosition.status == "OPEN"
        )
    )
    return int(res.scalar() or 0)


async def _symbol_open_notional(db: AsyncSession, user_id: int, symbol: str) -> float:
    res = await db.execute(
        select(
            func.coalesce(func.sum(LivePosition.quantity * LivePosition.entry_price), 0.0)
        ).where(
            LivePosition.user_id == user_id,
            LivePosition.symbol == symbol.upper(),
            LivePosition.status == "OPEN",
        )
    )
    return float(res.scalar() or 0.0)


async def _global_open_notional(db: AsyncSession) -> float:
    res = await db.execute(
        select(
            func.coalesce(func.sum(LivePosition.quantity * LivePosition.entry_price), 0.0)
        ).where(LivePosition.status == "OPEN")
    )
    return float(res.scalar() or 0.0)


# ── the gate ──────────────────────────────────────────────────────


async def beta_gate(
    db: AsyncSession, *, user_id: int, exchange: str, symbol: str, notional_usdt: float
) -> Optional[str]:
    """Return a block reason if `user_id` may NOT place this live order under the
    beta rules, else None. Only enforced when the beta is enabled."""
    if not beta_enabled():
        return None  # beta inactive → no additional restriction here

    member = await get_member(db, user_id)
    if member is None or member.status != APPROVED:
        return "not an approved live-beta member"
    if member.risk_agreement_accepted_at is None:
        return "risk agreement not accepted"

    if exchange.lower() not in [
        e.strip().lower() for e in member.allowed_exchanges.split(",") if e
    ]:
        return f"exchange {exchange} not allowed for this member"

    n = float(notional_usdt or 0.0)
    if n <= 0:
        return "invalid notional"

    if await _user_open_positions(db, user_id) >= member.max_positions:
        return f"max open positions reached ({member.max_positions})"
    if (await _user_open_notional(db, user_id)) + n > member.max_notional:
        return f"per-user capital limit exceeded ({member.max_notional} USDT)"
    if (
        await _symbol_open_notional(db, user_id, symbol)
    ) + n > settings.live_beta_per_symbol_max_notional:
        return f"per-symbol exposure limit exceeded ({settings.live_beta_per_symbol_max_notional} USDT)"
    if (await _global_open_notional(db)) + n > settings.live_beta_global_max_notional:
        return f"global beta exposure cap exceeded ({settings.live_beta_global_max_notional} USDT)"
    return None


def member_dict(m: LiveBetaMember) -> dict:
    return {
        "user_id": m.user_id,
        "status": m.status,
        "max_notional": m.max_notional,
        "max_positions": m.max_positions,
        "allowed_exchanges": [e for e in (m.allowed_exchanges or "").split(",") if e],
        "risk_agreement_accepted_at": (
            m.risk_agreement_accepted_at.isoformat() if m.risk_agreement_accepted_at else None
        ),
        "approved_by": m.approved_by,
        "approved_at": m.approved_at.isoformat() if m.approved_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
