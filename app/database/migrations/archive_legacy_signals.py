"""
Sprint 1 — Legacy Cleanup Migration
====================================
archive_legacy_signals.py

Moves legacy signals out of the production `signals` table into `archive_signals`.

LEGACY SIGNAL DEFINITION
    A signal is considered legacy if ANY of the following are true:
      1. timeframe = '5m'               (pre-MTF scanner generation)
      2. strategy  != 'MTF_SMC_STRICT'  (any old engine strategy string)

    The third condition from the spec ("confidence engine version < current MTF
    engine") cannot be queried directly because older records have no version tag.
    Condition 2 covers this: all signals generated before the MTF_SMC_STRICT
    engine have a different strategy value.

SAFETY
    Records are NEVER deleted. They are copied to archive_signals then removed
    from production. A duplicate-ID guard prevents double-archiving when the
    script is re-run.

USAGE
    # Local (needs DB reachable at POSTGRES_HOST):
    python -m app.database.migrations.archive_legacy_signals

    # Inside running docker stack:
    docker compose exec bot python -m app.database.migrations.archive_legacy_signals
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select, text

from app.database.models import ArchivedSignal, Base, DailyStat, Signal, WeeklyStat
from app.database.session import SessionLocal, engine

MTF_STRATEGY   = "MTF_SMC_STRICT"
MTF_TIMEFRAMES = {"15m", "1h", "4h", "1d"}
LEGACY_TF      = "5m"


# ─── helpers ────────────────────────────────────────────────────────────────

def _sep(char: str = "─", width: int = 60) -> None:
    print(char * width)


def _h(title: str) -> None:
    _sep()
    print(f"  {title}")
    _sep()


# ─── audit ──────────────────────────────────────────────────────────────────

async def _audit() -> dict:
    """
    Read and return a breakdown of every signal in the production table.
    Does NOT modify any data.
    """
    async with SessionLocal() as session:
        total_res = await session.execute(select(func.count(Signal.id)))
        total: int = int(total_res.scalar() or 0)

        by_tf_res = await session.execute(
            select(Signal.timeframe, func.count(Signal.id))
            .group_by(Signal.timeframe)
            .order_by(Signal.timeframe)
        )
        by_tf = {row[0]: row[1] for row in by_tf_res.all()}

        by_strat_res = await session.execute(
            select(Signal.strategy, func.count(Signal.id))
            .group_by(Signal.strategy)
            .order_by(func.count(Signal.id).desc())
        )
        by_strat = {row[0]: row[1] for row in by_strat_res.all()}

        conf_res = await session.execute(
            select(
                func.min(Signal.confidence),
                func.max(Signal.confidence),
                func.avg(Signal.confidence),
            )
        )
        conf_min, conf_max, conf_avg = conf_res.one()

        legacy_res = await session.execute(
            select(func.count(Signal.id)).where(
                (Signal.timeframe == LEGACY_TF) | (Signal.strategy != MTF_STRATEGY)
            )
        )
        legacy_count: int = int(legacy_res.scalar() or 0)

        # Already archived?
        arch_res = await session.execute(select(func.count(ArchivedSignal.id)))
        already_archived: int = int(arch_res.scalar() or 0)

    return {
        "total": total,
        "by_tf": by_tf,
        "by_strat": by_strat,
        "conf_min": round(float(conf_min or 0), 1),
        "conf_max": round(float(conf_max or 0), 1),
        "conf_avg": round(float(conf_avg or 0), 1),
        "legacy_count": legacy_count,
        "already_archived": already_archived,
    }


def _print_audit(a: dict) -> None:
    _h("DATABASE AUDIT — signals table")
    print(f"  Total signals in production:  {a['total']}")
    print(f"  Already in archive_signals:   {a['already_archived']}")
    print(f"  Legacy signals to archive:    {a['legacy_count']}")
    print()

    print("  Breakdown by timeframe:")
    for tf, cnt in sorted(a["by_tf"].items()):
        tag = " ← LEGACY" if tf == LEGACY_TF else (" ✓ MTF" if tf in MTF_TIMEFRAMES else "")
        print(f"    {tf:>6}  {cnt:>5}{tag}")
    print()

    print("  Breakdown by strategy:")
    for strat, cnt in a["by_strat"].items():
        tag = " ✓ CURRENT" if strat == MTF_STRATEGY else " ← LEGACY"
        print(f"    {strat or '(empty)':40}  {cnt:>5}{tag}")
    print()

    print(f"  Confidence range: {a['conf_min']}% – {a['conf_max']}%  (avg {a['conf_avg']}%)")
    print()

    _sep()
    print("  Tables present in ORM:")
    for tbl in ["signals", "archive_signals", "daily_stats", "weekly_stats",
                "watchlist", "users", "system_settings", "affiliate_clicks",
                "signal_messages", "paper_positions"]:
        print(f"    {tbl}")
    _sep()


# ─── migration ──────────────────────────────────────────────────────────────

async def _archive(dry_run: bool = False) -> tuple[int, int]:
    """
    Copy legacy signals to archive_signals, then remove from signals.
    Returns (archived_count, skipped_already_archived).
    Skips signals whose original_id is already in archive_signals (idempotent).
    """
    async with SessionLocal() as session:
        # Fetch all legacy signals
        result = await session.execute(
            select(Signal)
            .where(
                (Signal.timeframe == LEGACY_TF) | (Signal.strategy != MTF_STRATEGY)
            )
            .order_by(Signal.id)
        )
        legacy: list[Signal] = list(result.scalars().all())

        if not legacy:
            return 0, 0

        # Fetch IDs already archived (idempotency guard)
        existing_res = await session.execute(
            select(ArchivedSignal.original_id)
        )
        already_archived_ids: set[int] = {row[0] for row in existing_res.all()}

    to_archive = [s for s in legacy if s.id not in already_archived_ids]
    skipped    = len(legacy) - len(to_archive)

    if dry_run:
        return len(to_archive), skipped

    if not to_archive:
        return 0, skipped

    # Step 1: insert into archive_signals
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        for sig in to_archive:
            if sig.timeframe == LEGACY_TF:
                reason = "legacy_5m"
            elif sig.strategy != MTF_STRATEGY:
                reason = f"legacy_engine:{sig.strategy or 'unknown'}"
            else:
                reason = "legacy_unknown"

            arch = ArchivedSignal(
                original_id=sig.id,
                archive_reason=reason,
                archived_at=now,
                # --- all original Signal columns ---
                symbol=sig.symbol,
                side=sig.side,
                timeframe=sig.timeframe,
                confidence=sig.confidence,
                risk_level=sig.risk_level,
                strategy=sig.strategy,
                reasons=sig.reasons or "",
                entry_low=sig.entry_low,
                entry_high=sig.entry_high,
                tp1=sig.tp1,
                tp2=sig.tp2,
                tp3=sig.tp3,
                stop_loss=sig.stop_loss,
                risk_reward=sig.risk_reward,
                status=sig.status,
                pnl_pct=float(sig.pnl_pct or 0),
                max_favorable_pct=float(sig.max_favorable_pct or 0),
                max_adverse_pct=float(sig.max_adverse_pct or 0),
                telegram_message_id=sig.telegram_message_id,
                trend_score=sig.trend_score,
                structure_score=sig.structure_score,
                setup_score=sig.setup_score,
                entry_score=sig.entry_score,
                created_at=sig.created_at,
                closed_at=sig.closed_at,
            )
            session.add(arch)
        await session.commit()

    # Step 2: remove from production signals
    archived_ids = [s.id for s in to_archive]
    async with SessionLocal() as session:
        for sig_id in archived_ids:
            sig = await session.get(Signal, sig_id)
            if sig is not None:
                await session.delete(sig)
        await session.commit()

    return len(to_archive), skipped


# ─── main ────────────────────────────────────────────────────────────────────

async def run_migration(dry_run: bool = False) -> None:
    print()
    _h("ARGUS QUANT — SPRINT 1 LEGACY CLEANUP MIGRATION")

    # Ensure all tables exist before we start
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("  ✓ Database schema verified / created")
    print()

    # Audit current state
    audit = await _audit()
    _print_audit(audit)
    print()

    if audit["legacy_count"] == 0:
        print("  ✅ No legacy signals found — production table is already clean.")
        print()
        return

    if dry_run:
        print(f"  DRY-RUN MODE — would archive {audit['legacy_count']} signal(s), no changes made.")
        print()
        return

    # Archive
    print(f"  Archiving {audit['legacy_count']} legacy signal(s)…")
    archived, skipped = await _archive(dry_run=False)
    print(f"  ✓ Archived:  {archived}")
    if skipped:
        print(f"  ✓ Skipped (already archived): {skipped}")

    # Post-migration audit
    post = await _audit()
    print()
    _h("POST-MIGRATION STATE")
    print(f"  Production signals remaining:  {post['total']}")
    print(f"  Archive signals total:         {post['already_archived']}")
    print()
    print("  Production timeframes after migration:")
    for tf, cnt in sorted(post["by_tf"].items()):
        print(f"    {tf:>6}  {cnt:>5}")
    print()

    if post["legacy_count"] == 0:
        print("  ✅ Production table is clean — only MTF signals remain.")
    else:
        print(f"  ⚠ {post['legacy_count']} legacy signal(s) still remain (check logs).")

    print()
    _sep("═")
    print("  MIGRATION COMPLETE")
    print(f"  Archived today:   {archived}")
    print(f"  Production clean: {'YES' if post['legacy_count'] == 0 else 'NO'}")
    _sep("═")
    print()
    print("  Next steps:")
    print("    docker compose exec bot python scripts/rebuild_performance.py")
    print()


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    if dry:
        print("  *** DRY-RUN mode: no changes will be made ***")
    asyncio.run(run_migration(dry_run=dry))
