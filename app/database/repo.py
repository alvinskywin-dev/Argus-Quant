"""
Repository helpers — typed, async, transaction-safe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Optional

from sqlalchemy import and_, delete, desc, func, select, update

from app.database.models import DailyStat, Signal, SignalMessage, SystemSetting, User, Watchlist
from app.database.session import get_session

# Statuses that mean a signal is still active (position is open)
ACTIVE_STATUSES: list[str] = ["OPEN", "ACTIVE", "PENDING"]


# ---------------- duplicate-signal guard ----------------

async def has_active_signal(symbol: str, side: str | None = None) -> bool:
    """
    Return True if there is at least one active (OPEN/ACTIVE/PENDING) signal
    for *symbol*.  Pass *side* to restrict to a specific direction.
    """
    async with get_session() as s:
        q = (
            select(Signal.id)
            .where(
                Signal.symbol == symbol,
                Signal.status.in_(ACTIVE_STATUSES),
            )
        )
        if side is not None:
            q = q.where(Signal.side == side)
        row = await s.execute(q.limit(1))
        return row.scalar_one_or_none() is not None


async def has_active_signal_excluding(symbol: str, exclude_id: int) -> bool:
    """
    Same as *has_active_signal* but ignores the signal with *exclude_id*.
    Used by the publisher guard to avoid blocking the signal that was JUST created.
    """
    async with get_session() as s:
        row = await s.execute(
            select(Signal.id)
            .where(
                Signal.symbol == symbol,
                Signal.status.in_(ACTIVE_STATUSES),
                Signal.id != exclude_id,
            )
            .limit(1)
        )
        return row.scalar_one_or_none() is not None


async def in_post_close_cooldown(symbol: str, side: str, hours: int) -> bool:
    """
    Return True if a closed (TP/SL) signal for *symbol*+*side* was closed within
    the last *hours* hours.  Enforces a re-entry silence period after a trade ends.
    Returns False when *hours* <= 0 (feature disabled).
    """
    if hours <= 0:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with get_session() as s:
        row = await s.execute(
            select(Signal.id)
            .where(
                Signal.symbol == symbol,
                Signal.side == side,
                Signal.status.in_(["TP1", "TP2", "TP3", "SL"]),
                Signal.closed_at >= cutoff,
            )
            .limit(1)
        )
        return row.scalar_one_or_none() is not None


async def get_active_signals_summary() -> list[dict[str, Any]]:
    """Return one row per symbol that currently has an active signal."""
    async with get_session() as s:
        rows = await s.execute(
            select(
                Signal.symbol,
                Signal.side,
                Signal.status,
                Signal.confidence,
                Signal.created_at,
            )
            .where(Signal.status.in_(ACTIVE_STATUSES))
            .order_by(desc(Signal.created_at))
        )
        return [
            {
                "symbol":     r[0],
                "side":       r[1],
                "status":     r[2],
                "confidence": round(float(r[3] or 0), 1),
                "opened":     r[4].strftime("%m-%d %H:%M") if r[4] else "-",
            }
            for r in rows.all()
        ]


# ---------------- signals ----------------
async def create_signal(data: dict[str, Any]) -> Signal:
    async with get_session() as s:
        sig = Signal(**data)
        s.add(sig)
        await s.flush()
        await s.refresh(sig)
        return sig


async def update_signal(signal_id: int, fields: dict[str, Any]) -> None:
    async with get_session() as s:
        await s.execute(update(Signal).where(Signal.id == signal_id).values(**fields))


async def get_open_signals() -> List[Signal]:
    async with get_session() as s:
        rows = await s.execute(
            select(Signal).where(Signal.status.in_(["OPEN", "TP1", "TP2"]))
        )
        return list(rows.scalars().all())


async def get_recent_signals(limit: int = 20) -> List[Signal]:
    async with get_session() as s:
        rows = await s.execute(
            select(Signal).order_by(desc(Signal.created_at)).limit(limit)
        )
        return list(rows.scalars().all())


async def last_signal_for(symbol: str, side: str) -> Optional[Signal]:
    async with get_session() as s:
        rows = await s.execute(
            select(Signal)
            .where(and_(Signal.symbol == symbol, Signal.side == side))
            .order_by(desc(Signal.created_at))
            .limit(1)
        )
        return rows.scalar_one_or_none()


async def count_signals_since(since: datetime) -> int:
    async with get_session() as s:
        rows = await s.execute(
            select(func.count(Signal.id)).where(Signal.created_at >= since)
        )
        return int(rows.scalar() or 0)


# ---------------- watchlist ----------------
async def add_watch(user_id: int, symbol: str) -> bool:
    async with get_session() as s:
        existing = await s.execute(
            select(Watchlist).where(
                and_(Watchlist.user_id == user_id, Watchlist.symbol == symbol)
            )
        )
        if existing.scalar_one_or_none():
            return False
        s.add(Watchlist(user_id=user_id, symbol=symbol))
        return True


async def remove_watch(user_id: int, symbol: str) -> bool:
    async with get_session() as s:
        res = await s.execute(
            delete(Watchlist).where(
                and_(Watchlist.user_id == user_id, Watchlist.symbol == symbol)
            )
        )
        return (res.rowcount or 0) > 0


async def list_watch(user_id: int) -> List[str]:
    async with get_session() as s:
        rows = await s.execute(
            select(Watchlist.symbol).where(Watchlist.user_id == user_id)
        )
        return [r[0] for r in rows.all()]


# ---------------- users ----------------
async def upsert_user(user_id: int, username: str | None, is_admin: bool = False) -> User:
    async with get_session() as s:
        u = await s.get(User, user_id)
        if u is None:
            u = User(id=user_id, username=username, is_admin=is_admin)
            s.add(u)
        else:
            u.username = username or u.username
            if is_admin:
                u.is_admin = True
        await s.flush()
        return u


# ---------------- system settings ----------------
async def get_setting(key: str, default: str | None = None) -> str | None:
    async with get_session() as s:
        row = await s.get(SystemSetting, key)
        return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    async with get_session() as s:
        row = await s.get(SystemSetting, key)
        if row is None:
            s.add(SystemSetting(key=key, value=value))
        else:
            row.value = value


# ---------------- stats ----------------
async def winrate_summary(days: int = 7) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with get_session() as s:
        rows = await s.execute(
            select(
                func.count(Signal.id),
                func.sum(func.case((Signal.status.in_(["TP1", "TP2", "TP3"]), 1), else_=0)),
                func.sum(func.case((Signal.status == "SL", 1), else_=0)),
                func.avg(Signal.pnl_pct),
            ).where(Signal.created_at >= since, Signal.status != "OPEN")
        )
        total, wins, losses, avg_pnl = rows.one()
        total = int(total or 0)
        wins = int(wins or 0)
        losses = int(losses or 0)
        wr = (wins / total * 100.0) if total else 0.0
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "winrate": wr,
            "avg_pnl": float(avg_pnl or 0.0),
        }


async def leaderboard(limit: int = 10) -> List[dict[str, Any]]:
    async with get_session() as s:
        rows = await s.execute(
            select(
                Signal.symbol,
                func.count(Signal.id).label("cnt"),
                func.avg(Signal.pnl_pct).label("avg_pnl"),
            )
            .where(Signal.status != "OPEN")
            .group_by(Signal.symbol)
            .order_by(desc("avg_pnl"))
            .limit(limit)
        )
        return [
            {"symbol": r[0], "signals": int(r[1]), "avg_pnl": float(r[2] or 0.0)}
            for r in rows.all()
        ]


# ---------------- signal messages ----------------
async def save_signal_message(signal_id: int, chat_id: str, telegram_message_id: int) -> None:
    async with get_session() as s:
        s.add(
            SignalMessage(
                signal_id=signal_id,
                chat_id=str(chat_id),
                telegram_message_id=int(telegram_message_id),
            )
        )


async def get_signal_messages(signal_id: int) -> list[SignalMessage]:
    async with get_session() as s:
        rows = await s.execute(
            select(SignalMessage).where(SignalMessage.signal_id == signal_id)
        )
        return list(rows.scalars().all())
