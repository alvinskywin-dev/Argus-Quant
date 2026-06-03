"""
Sprint 20E — safety-layer business logic.

Wraps every auto open with account-protection checks and exposes user/admin
kill switches. Pure maths is in app.safety.rules; this module does the DB
aggregation and state mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PaperAccount,
    PaperAccountPosition,
    PaperTrade,
    SafetyConfig,
    SafetyState,
    SystemSetting,
)
from app.safety import rules
from app.utils.logger import logger

_GLOBAL_KILL_KEY = "trading_global_kill"


class SafetyError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class SafetyDecision:
    allow: bool
    reason: str = "ok"
    code: str = "ok"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _midnight_next(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


# ── config / state ────────────────────────────────────────────────


async def get_or_create_config(db: AsyncSession, user_id: int) -> SafetyConfig:
    res = await db.execute(select(SafetyConfig).where(SafetyConfig.user_id == user_id))
    cfg = res.scalar_one_or_none()
    if cfg is None:
        cfg = SafetyConfig(user_id=user_id)
        db.add(cfg)
        await db.flush()
    return cfg


_UPDATABLE = {
    "max_daily_loss_pct",
    "max_weekly_loss_pct",
    "max_open_positions",
    "max_correlated_positions",
    "max_leverage",
    "trade_cooldown_minutes",
    "loss_streak_limit",
    "loss_streak_cooldown_hours",
}


async def update_config(db: AsyncSession, user_id: int, changes: dict) -> SafetyConfig:
    cfg = await get_or_create_config(db, user_id)
    for key, value in changes.items():
        if value is not None and key in _UPDATABLE:
            setattr(cfg, key, value)
    return cfg


async def get_or_create_state(db: AsyncSession, user_id: int) -> SafetyState:
    res = await db.execute(select(SafetyState).where(SafetyState.user_id == user_id))
    st = res.scalar_one_or_none()
    if st is None:
        st = SafetyState(user_id=user_id)
        db.add(st)
        await db.flush()
    return st


# ── kill switches ─────────────────────────────────────────────────


async def get_global_kill(db: AsyncSession) -> bool:
    row = await db.get(SystemSetting, _GLOBAL_KILL_KEY)
    return bool(row and row.value == "1")


async def set_global_kill(db: AsyncSession, on: bool) -> None:
    row = await db.get(SystemSetting, _GLOBAL_KILL_KEY)
    if row is None:
        row = SystemSetting(key=_GLOBAL_KILL_KEY, value="1" if on else "0")
        db.add(row)
    else:
        row.value = "1" if on else "0"
    logger.warning(f"[safety] GLOBAL kill switch -> {'ON' if on else 'OFF'}")


async def set_user_kill(db: AsyncSession, user_id: int, on: bool) -> SafetyState:
    st = await get_or_create_state(db, user_id)
    st.kill_switch = on
    if on:
        st.disabled_reason = "user kill switch"
    logger.warning(f"[safety] user={user_id} kill switch -> {'ON' if on else 'OFF'}")
    return st


async def resume_user(db: AsyncSession, user_id: int) -> SafetyState:
    """Clear the user kill switch and any timed lockout."""
    st = await get_or_create_state(db, user_id)
    st.kill_switch = False
    st.disabled_until = None
    st.disabled_reason = None
    return st


async def _disable(db: AsyncSession, user_id: int, until: datetime, reason: str) -> None:
    st = await get_or_create_state(db, user_id)
    # Extend, never shorten, an existing lockout.
    if st.disabled_until is None or until > st.disabled_until:
        st.disabled_until = until
        st.disabled_reason = reason
    logger.warning(f"[safety] user={user_id} trading disabled until {until} ({reason})")


# ── aggregation helpers ───────────────────────────────────────────


async def _realized_since(db: AsyncSession, account_id: int, since: datetime) -> float:
    rows = (
        (
            await db.execute(
                select(PaperTrade.pnl_usdt).where(
                    PaperTrade.account_id == account_id, PaperTrade.closed_at >= since
                )
            )
        )
        .scalars()
        .all()
    )
    return float(sum(rows))


async def _recent_pnls(db: AsyncSession, account_id: int, limit: int = 20) -> list[float]:
    rows = (
        (
            await db.execute(
                select(PaperTrade.pnl_usdt)
                .where(PaperTrade.account_id == account_id)
                .order_by(PaperTrade.closed_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [float(x) for x in rows]


async def _open_clusters_sides(db: AsyncSession, account_id: int) -> list[tuple[str, str]]:
    rows = (
        await db.execute(
            select(PaperAccountPosition.symbol, PaperAccountPosition.side).where(
                PaperAccountPosition.account_id == account_id,
                PaperAccountPosition.status == "OPEN",
            )
        )
    ).all()
    return [(rules.correlation_cluster(sym), side) for sym, side in rows]


async def _last_open_at(db: AsyncSession, account_id: int) -> Optional[datetime]:
    row = (
        await db.execute(
            select(PaperAccountPosition.opened_at)
            .where(PaperAccountPosition.account_id == account_id)
            .order_by(PaperAccountPosition.opened_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


# ── the check ─────────────────────────────────────────────────────


async def check(
    db: AsyncSession,
    *,
    user_id: int,
    account: PaperAccount,
    summary: dict,
    symbol: str,
    side: str,
) -> SafetyDecision:
    """Run all protective rules. May set a timed lockout as a side effect."""
    now = _now()

    if await get_global_kill(db):
        return SafetyDecision(False, "global emergency stop active", "global_kill")

    state = await get_or_create_state(db, user_id)
    if state.kill_switch:
        return SafetyDecision(False, "kill switch enabled", "user_kill")
    if state.disabled_until and state.disabled_until > now:
        return SafetyDecision(
            False,
            f"trading disabled until {state.disabled_until:%Y-%m-%d %H:%M} UTC "
            f"({state.disabled_reason})",
            "locked",
        )

    cfg = await get_or_create_config(db, user_id)
    init_bal = account.initial_balance

    # Daily / weekly loss limits -> lock until next UTC midnight.
    daily = await _realized_since(
        db, account.id, now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    if rules.loss_exceeds_limit(daily, init_bal, cfg.max_daily_loss_pct):
        await _disable(db, user_id, _midnight_next(now), "daily loss limit")
        return SafetyDecision(False, f"daily loss limit hit ({daily:.2f})", "daily_loss")

    weekly = await _realized_since(db, account.id, now - timedelta(days=7))
    if rules.loss_exceeds_limit(weekly, init_bal, cfg.max_weekly_loss_pct):
        await _disable(db, user_id, _midnight_next(now), "weekly loss limit")
        return SafetyDecision(False, f"weekly loss limit hit ({weekly:.2f})", "weekly_loss")

    # Loss streak -> lock for cooldown hours.
    streak = rules.consecutive_losses(await _recent_pnls(db, account.id))
    if cfg.loss_streak_limit > 0 and streak >= cfg.loss_streak_limit:
        await _disable(
            db,
            user_id,
            now + timedelta(hours=cfg.loss_streak_cooldown_hours),
            f"{streak} losses in a row",
        )
        return SafetyDecision(False, f"loss streak protection ({streak} losses)", "loss_streak")

    # Trade cooldown (transient, no lockout).
    if cfg.trade_cooldown_minutes > 0:
        last = await _last_open_at(db, account.id)
        if last and (now - last) < timedelta(minutes=cfg.trade_cooldown_minutes):
            return SafetyDecision(False, "trade cooldown active", "cooldown")

    # Max open positions.
    if summary["open_positions"] >= cfg.max_open_positions:
        return SafetyDecision(False, f"max open positions ({cfg.max_open_positions})", "max_open")

    # Max correlated positions (same cluster + same side).
    cluster = rules.correlation_cluster(symbol)
    correlated = rules.count_correlated(await _open_clusters_sides(db, account.id), cluster, side)
    if correlated >= cfg.max_correlated_positions:
        return SafetyDecision(
            False,
            f"max correlated positions in {cluster} ({cfg.max_correlated_positions})",
            "max_correlated",
        )

    return SafetyDecision(True, "ok", "ok")


async def trading_blocked(db: AsyncSession, user_id: int) -> Optional[str]:
    """
    Lightweight gate reusable by LIVE trading (Sprint 20F): checks the global
    emergency stop, the user kill switch, and any active timed lockout. Returns
    a reason string if blocked, else None. Does not evaluate loss limits (those
    need an account context and are applied in the demo engine's full check()).
    """
    if await get_global_kill(db):
        return "global emergency stop active"
    state = await get_or_create_state(db, user_id)
    if state.kill_switch:
        return "kill switch enabled"
    if state.disabled_until and state.disabled_until > _now():
        return f"trading disabled until {state.disabled_until:%Y-%m-%d %H:%M} UTC ({state.disabled_reason})"
    return None


# ── status (for API) ──────────────────────────────────────────────


async def status(db: AsyncSession, user_id: int, account: PaperAccount) -> dict:
    now = _now()
    state = await get_or_create_state(db, user_id)
    cfg = await get_or_create_config(db, user_id)
    daily = await _realized_since(
        db, account.id, now.replace(hour=0, minute=0, second=0, microsecond=0)
    )
    weekly = await _realized_since(db, account.id, now - timedelta(days=7))
    streak = rules.consecutive_losses(await _recent_pnls(db, account.id))
    locked = bool(state.disabled_until and state.disabled_until > now)
    return {
        "trading_enabled": not (await get_global_kill(db) or state.kill_switch or locked),
        "global_kill": await get_global_kill(db),
        "kill_switch": state.kill_switch,
        "disabled_until": state.disabled_until,
        "disabled_reason": state.disabled_reason if locked or state.kill_switch else None,
        "daily_pnl": round(daily, 2),
        "weekly_pnl": round(weekly, 2),
        "loss_streak": streak,
        "max_daily_loss_pct": cfg.max_daily_loss_pct,
        "max_weekly_loss_pct": cfg.max_weekly_loss_pct,
    }
