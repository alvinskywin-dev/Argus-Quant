"""
Migration: Backfill ``diagnostics.tp_history`` for legacy signals.

Old signals predate the ``tp_history`` lifecycle field, so a trade that reached
a take-profit and then drifted back to the stop loss was recorded with a final
``status = 'SL'`` and classified as a plain LOSS (see app.analytics.trade_outcome).

We can reconstruct the lifecycle from ``max_favorable_pct`` — the peak favourable
price move the tracker recorded while the trade was open. A take-profit level was
reached iff that peak move is at least the favourable distance to the level:

    max_favorable_pct >= price_move_pct(side, entry, tp_level)

with ``entry = (entry_low + entry_high) / 2`` (the same mid the tracker uses).

The reconstructed events are replayed through the *same* ``record_exit_event``
helper used live, so the resulting blob is identical in shape to a natively
tracked trade. The pass is **idempotent** (re-running changes nothing) and only
touches signals that actually reached a take-profit (``max_tp >= 1``) — genuine
losses and never-triggered signals already classify correctly from ``status``.

Usage:
    python -m app.database.migrations.backfill_tp_history            # dry-run
    python -m app.database.migrations.backfill_tp_history --commit   # write
    # inside docker:
    docker compose exec bot python -m app.database.migrations.backfill_tp_history --commit
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from sqlalchemy import select

from app.analytics.trade_outcome import (
    _TP_RANK,
    classify_trade_outcome,
    outcome_label,
    record_exit_event,
)
from app.database.models import Signal
from app.database.session import SessionLocal
from app.paper_engine.math import price_move_pct

# Float slack to absorb the 3-decimal rounding applied to max_favorable_pct.
_EPS = 1e-6


def _coerce(diagnostics: Any) -> dict:
    if isinstance(diagnostics, dict):
        return dict(diagnostics)
    if isinstance(diagnostics, str) and diagnostics:
        try:
            parsed = json.loads(diagnostics)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _infer_max_tp(sig: Signal) -> int:
    """Highest take-profit ever reached, from the recorded peak favourable move
    combined with the (authoritative) status rank."""
    status_rank = _TP_RANK.get((sig.status or "").upper(), 0)

    entry = (float(sig.entry_low or 0) + float(sig.entry_high or 0)) / 2.0
    if entry <= 0:
        return status_rank

    fav = float(sig.max_favorable_pct or 0.0)
    inferred = 0
    for level, raw in ((1, sig.tp1), (2, sig.tp2), (3, sig.tp3)):
        tp = float(raw or 0)
        if tp <= 0:
            continue
        need = price_move_pct(sig.side, entry, tp)  # favourable % to reach the level
        if need > 0 and fav + _EPS >= need:
            inferred = level

    return max(inferred, status_rank)


def _rebuild_diag(sig: Signal) -> dict | None:
    """Return the updated diagnostics dict, or ``None`` if nothing to backfill."""
    max_tp = _infer_max_tp(sig)
    if max_tp < 1:
        # Genuine loss / never-triggered / still-open-no-TP: status fallback is
        # already correct — leave the row untouched.
        return None

    sl_hit = (sig.status or "").upper() == "SL"
    final_pnl = float(sig.pnl_pct or 0.0)
    diag = _coerce(sig.diagnostics)

    # Replay the lifecycle in order: the highest take-profit reached (which
    # cascades to fill the lower levels), then the stop if it later closed at SL.
    diag = record_exit_event(diag, f"TP{max_tp}", event_time=sig.created_at, realized_pnl=final_pnl)
    if sl_hit:
        diag = record_exit_event(
            diag, "SL", event_time=sig.closed_at or sig.created_at, realized_pnl=final_pnl
        )

    diag["tp_history"]["backfilled"] = True
    return diag


async def run_migration(commit: bool) -> None:
    async with SessionLocal() as session:
        result = await session.execute(select(Signal).order_by(Signal.id))
        signals: list[Signal] = list(result.scalars().all())

    changed: list[tuple[Signal, dict]] = []
    flipped = 0  # rows whose winrate bucket changes LOSS -> WIN
    for sig in signals:
        new_diag = _rebuild_diag(sig)
        if new_diag is None:
            continue
        before = json.dumps(_coerce(sig.diagnostics), sort_keys=True)
        after = json.dumps(new_diag, sort_keys=True)
        if before == after:
            continue  # idempotent: already backfilled / live-tracked

        before_oc = classify_trade_outcome(
            status=sig.status, realized_pnl=sig.pnl_pct, diagnostics=sig.diagnostics
        )
        after_oc = classify_trade_outcome(
            status=sig.status, realized_pnl=sig.pnl_pct, diagnostics=new_diag
        )
        if before_oc.winrate_bucket == "LOSS" and after_oc.winrate_bucket == "WIN":
            flipped += 1
        changed.append((sig, new_diag))

    print(f"Scanned {len(signals)} signals — {len(changed)} need tp_history backfill.")
    print(f"  ↳ {flipped} currently counted as LOSS would become WIN (TP-then-SL).")
    print()

    for sig, new_diag in changed[:15]:
        oc = classify_trade_outcome(status=sig.status, realized_pnl=sig.pnl_pct, diagnostics=new_diag)
        print(
            f"  #{sig.id} {sig.symbol} {sig.side} status={sig.status} "
            f"max_fav={sig.max_favorable_pct}% -> max_tp={oc.max_tp_hit} "
            f"{outcome_label(oc.outcome)}"
        )
    if len(changed) > 15:
        print(f"  … and {len(changed) - 15} more.")
    print()

    if not commit:
        print("DRY-RUN — no rows written. Re-run with --commit to apply.")
        return

    if not changed:
        print("Nothing to write.")
        return

    async with SessionLocal() as session:
        for sig, new_diag in changed:
            row = await session.get(Signal, sig.id)
            if row is None:
                continue
            row.diagnostics = json.dumps(new_diag)
        await session.commit()

    print(f"✅ Backfilled tp_history for {len(changed)} signals.")


if __name__ == "__main__":
    asyncio.run(run_migration(commit="--commit" in sys.argv))
