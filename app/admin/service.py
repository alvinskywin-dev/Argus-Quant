"""
Sprint 20H — Admin Dashboard service (platform oversight).

Read-oriented aggregation across the multi-user SaaS tables, plus two safe
moderation actions (suspend / activate a user). ADMIN-role only — the router
enforces it; this module assumes the caller is already authorised.

SAFETY: this layer NEVER returns decrypted credentials. Exchange accounts are
surfaced by exchange + status + api_key_last4 only. Global emergency stop lives
in the 20E safety admin API and is reused here (read-only) rather than copied.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AuthUser,
    AutoTradeConfig,
    ExchangeAccount,
    LiveAuditLog,
    LivePosition,
    LiveTrade,
    SafetyState,
)
from app.exchange_adapters import live_gate_open
from app.safety import service as safety


class AdminError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _count(db: AsyncSession, stmt) -> int:
    return int((await db.execute(stmt)).scalar() or 0)


async def _group_counts(db: AsyncSession, column) -> dict[str, int]:
    rows = (await db.execute(select(column, func.count()).group_by(column))).all()
    return {str(k): int(v) for k, v in rows}


# ── platform overview ─────────────────────────────────────────────


async def overview(db: AsyncSession) -> dict:
    """Platform-wide rollup for the admin landing view."""
    users_by_role = await _group_counts(db, AuthUser.role)
    users_by_status = await _group_counts(db, AuthUser.status)
    exch_by_exchange = await _group_counts(
        db,
        ExchangeAccount.exchange,
    )
    connected_exch = await _count(
        db,
        select(func.count())
        .select_from(ExchangeAccount)
        .where(ExchangeAccount.status == "CONNECTED"),
    )

    open_positions = await _count(
        db, select(func.count()).select_from(LivePosition).where(LivePosition.status == "OPEN")
    )
    open_live = await _count(
        db,
        select(func.count())
        .select_from(LivePosition)
        .where(LivePosition.status == "OPEN", LivePosition.mode == "LIVE"),
    )
    auto_enabled = await _count(
        db,
        select(func.count()).select_from(AutoTradeConfig).where(AutoTradeConfig.enabled.is_(True)),
    )
    kill_switches = await _count(
        db, select(func.count()).select_from(SafetyState).where(SafetyState.kill_switch.is_(True))
    )

    realized = (
        await db.execute(select(func.coalesce(func.sum(LiveTrade.pnl_usdt), 0.0)))
    ).scalar() or 0.0

    return {
        "users": {
            "total": sum(users_by_role.values()),
            "by_role": users_by_role,
            "by_status": users_by_status,
        },
        "exchange_accounts": {
            "by_exchange": exch_by_exchange,
            "connected": connected_exch,
        },
        "positions": {
            "open_total": open_positions,
            "open_live": open_live,
            "open_mock": open_positions - open_live,
        },
        "auto_trading_enabled_users": auto_enabled,
        "user_kill_switches_active": kill_switches,
        "realized_pnl_usdt": round(float(realized), 2),
        "global_kill": await safety.get_global_kill(db),
        "live_gate_open": live_gate_open(),
        "generated_at": _now().isoformat(),
    }


# ── user listing & detail ─────────────────────────────────────────


async def list_users(
    db: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    role: Optional[str] = None,
) -> dict:
    q = select(AuthUser)
    if status:
        q = q.where(AuthUser.status == status.upper())
    if role:
        q = q.where(AuthUser.role == role.upper())
    total = await _count(db, select(func.count()).select_from(q.subquery()))
    q = q.order_by(AuthUser.created_at.desc()).limit(min(limit, 500)).offset(max(offset, 0))
    users = list((await db.execute(q)).scalars().all())

    # Per-user summary counts (avoid N+1 with grouped queries over the page).
    ids = [u.id for u in users]
    exch_counts = await _per_user_count(
        db, ExchangeAccount, ids, where=ExchangeAccount.status == "CONNECTED"
    )
    auto_on = await _per_user_flag(db, AutoTradeConfig, ids, AutoTradeConfig.enabled)
    kills = await _per_user_flag(db, SafetyState, ids, SafetyState.kill_switch)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "username": u.username,
                "role": u.role,
                "status": u.status,
                "is_verified": u.is_verified,
                "provider": getattr(u, "provider", None) or "email",
                "timezone": getattr(u, "timezone", None) or "UTC",
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "connected_exchanges": exch_counts.get(u.id, 0),
                "auto_trading": bool(auto_on.get(u.id, False)),
                "kill_switch": bool(kills.get(u.id, False)),
            }
            for u in users
        ],
    }


async def _per_user_count(db, model, ids, *, where=None) -> dict[int, int]:
    if not ids:
        return {}
    q = select(model.user_id, func.count()).where(model.user_id.in_(ids))
    if where is not None:
        q = q.where(where)
    q = q.group_by(model.user_id)
    return {int(uid): int(n) for uid, n in (await db.execute(q)).all()}


async def _per_user_flag(db, model, ids, column) -> dict[int, bool]:
    if not ids:
        return {}
    rows = (await db.execute(select(model.user_id, column).where(model.user_id.in_(ids)))).all()
    return {int(uid): bool(val) for uid, val in rows}


async def user_detail(db: AsyncSession, user_id: int) -> dict:
    user = await db.get(AuthUser, user_id)
    if user is None:
        raise AdminError(404, "User not found")

    accounts = list(
        (
            await db.execute(
                select(ExchangeAccount)
                .where(ExchangeAccount.user_id == user_id)
                .order_by(ExchangeAccount.exchange)
            )
        )
        .scalars()
        .all()
    )
    auto = (
        await db.execute(select(AutoTradeConfig).where(AutoTradeConfig.user_id == user_id))
    ).scalar_one_or_none()
    state = (
        await db.execute(select(SafetyState).where(SafetyState.user_id == user_id))
    ).scalar_one_or_none()
    positions = list(
        (
            await db.execute(
                select(LivePosition)
                .where(LivePosition.user_id == user_id, LivePosition.status == "OPEN")
                .order_by(LivePosition.opened_at.desc())
            )
        )
        .scalars()
        .all()
    )

    return {
        "profile": {
            "id": user.id,
            "email": user.email,
            "username": user.username,
            "role": user.role,
            "status": user.status,
            "is_verified": user.is_verified,
            "totp_enabled": user.totp_enabled,
            "provider": getattr(user, "provider", None) or "email",
            "timezone": getattr(user, "timezone", None) or "UTC",
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        # SAFETY: last4 + status only — never the encrypted/decrypted secret.
        "exchange_accounts": [
            {
                "exchange": a.exchange,
                "label": a.label,
                "status": a.status,
                "api_key_last4": a.api_key_last4,
                "can_trade": a.can_trade,
                "can_futures": a.can_futures,
                "can_withdraw": a.can_withdraw,
            }
            for a in accounts
        ],
        "auto_trade": (
            None
            if auto is None
            else {
                "enabled": auto.enabled,
                "max_positions": auto.max_positions,
                "max_leverage": auto.max_leverage,
                "risk_per_trade_pct": auto.risk_per_trade_pct,
                "allowed_exchanges": auto.allowed_exchanges,
                "min_confidence": auto.min_confidence,
            }
        ),
        "safety_state": (
            None
            if state is None
            else {
                "kill_switch": state.kill_switch,
                "disabled_until": (
                    state.disabled_until.isoformat() if state.disabled_until else None
                ),
                "disabled_reason": state.disabled_reason,
            }
        ),
        "open_positions": [
            {
                "id": p.id,
                "exchange": p.exchange,
                "symbol": p.symbol,
                "side": p.side,
                "quantity": p.quantity,
                "entry_price": p.entry_price,
                "leverage": p.leverage,
                "mode": p.mode,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
            }
            for p in positions
        ],
    }


# ── audit feed ────────────────────────────────────────────────────


async def audit_feed(
    db: AsyncSession, *, limit: int = 100, user_id: Optional[int] = None
) -> list[dict]:
    q = select(LiveAuditLog)
    if user_id is not None:
        q = q.where(LiveAuditLog.user_id == user_id)
    q = q.order_by(LiveAuditLog.created_at.desc()).limit(min(limit, 500))
    rows = list((await db.execute(q)).scalars().all())
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "exchange": r.exchange,
            "symbol": r.symbol,
            "action": r.action,
            "result": r.result,
            "mode": r.mode,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ── moderation ────────────────────────────────────────────────────


async def set_user_status(db: AsyncSession, *, admin_id: int, user_id: int, status: str) -> dict:
    """Suspend or re-activate a user. Admins cannot suspend themselves."""
    status = status.upper()
    if status not in ("ACTIVE", "SUSPENDED"):
        raise AdminError(400, "status must be ACTIVE or SUSPENDED")
    if user_id == admin_id and status == "SUSPENDED":
        raise AdminError(400, "Admins cannot suspend their own account")
    user = await db.get(AuthUser, user_id)
    if user is None:
        raise AdminError(404, "User not found")
    user.status = status
    await db.flush()
    return {"id": user.id, "email": user.email, "status": user.status}
