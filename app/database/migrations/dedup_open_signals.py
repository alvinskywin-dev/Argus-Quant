"""
Migration: Deduplicate OPEN signals (prerequisite for the partial unique index).

Finds symbols that have more than one OPEN signal and keeps only the most
recent one, setting older duplicates to status='EXPIRED'.

This migration MUST be run before the partial unique index
`uq_active_signal_symbol` can be created on the signals table.

Usage:
    docker compose exec bot python -m app.database.migrations.dedup_open_signals
"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from sqlalchemy import select, update

from app.database.models import Base, Signal
from app.database.session import SessionLocal, engine


async def run() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        res = await session.execute(
            select(Signal)
            .where(Signal.status.in_(["OPEN", "ACTIVE", "PENDING"]))
            .order_by(Signal.symbol, Signal.created_at.desc())
        )
        active: list[Signal] = list(res.scalars().all())

    # Group by symbol; keep the FIRST (most recent, since ordered desc) per symbol
    by_symbol: dict[str, list[Signal]] = defaultdict(list)
    for s in active:
        by_symbol[s.symbol].append(s)

    to_expire: list[int] = []
    for sym, sigs in by_symbol.items():
        if len(sigs) > 1:
            # Keep sigs[0] (most recent), expire the rest
            for dup in sigs[1:]:
                to_expire.append(dup.id)

    if not to_expire:
        print("✅ No duplicate OPEN signals found — database is already clean.")
        return

    print(f"Found {len(to_expire)} duplicate OPEN signal(s) to expire:")
    async with SessionLocal() as session:
        for sig_id in to_expire:
            await session.execute(
                update(Signal).where(Signal.id == sig_id).values(status="EXPIRED")
            )
        await session.commit()

    print(f"✅ Set {len(to_expire)} duplicate(s) to EXPIRED.")
    print()
    print("You can now retry the partial unique index creation:")
    print("  docker compose restart bot")


if __name__ == "__main__":
    asyncio.run(run())
