"""
Trade Outcome Backfill (winrate fix)
====================================
scripts/backfill_trade_outcomes.py

Non-destructive backfill of lifecycle-aware ``trade_outcome`` into each signal's
``diagnostics.tp_history`` JSON. It never rewrites status, timestamps, PnL, or
any execution state — it only *adds* a tp_history block so historical analytics
classify TP-then-SL trades as (partial) wins instead of losses.

How max_tp_hit is inferred for legacy rows (where the original tracker erased the
TP hit by overwriting status to SL):

  1. If diagnostics already has tp_history.max_tp_hit -> keep it (authoritative).
  2. From the latest status (TP1/TP2/TP3 -> 1/2/3).
  3. Heuristic recovery: compare the stored max_favorable_pct against the TP
     levels. If price ran far enough in favour to touch TP1/TP2/TP3 at any point
     (even though it later stopped out), credit that level. This is what recovers
     the winrate that the original bug hid.

Usage:
    # safe preview (default) — prints what WOULD change, writes nothing
    python -m scripts.backfill_trade_outcomes
    python -m scripts.backfill_trade_outcomes --dry-run

    # actually persist the tp_history backfill
    python -m scripts.backfill_trade_outcomes --apply

    # limit scope
    python -m scripts.backfill_trade_outcomes --apply --days 90
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

# Make app/ importable when run as a standalone script outside the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, update  # noqa: E402

from app.analytics.trade_outcome import classify_trade_outcome  # noqa: E402
from app.database.models import Signal  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402

_TP_RANK = {"TP1": 1, "TP2": 2, "TP3": 3}


def _favorable_pct_to(level: float, entry_mid: float, side: str) -> float:
    """Signed favourable % move from entry to a price level for *side*."""
    if not entry_mid:
        return 0.0
    raw = (level - entry_mid) / entry_mid * 100.0
    return raw if (side or "").upper() == "LONG" else -raw


def infer_max_tp_hit(sig: Signal, existing: dict) -> tuple[int, str]:
    """Return (max_tp_hit, method) for a signal, non-destructively.

    ``existing`` is the parsed tp_history (may be empty).
    """
    if isinstance(existing.get("max_tp_hit"), int) and existing["max_tp_hit"] > 0:
        return existing["max_tp_hit"], "tp_history"

    by_status = _TP_RANK.get((sig.status or "").upper(), 0)

    # Heuristic recovery from max favourable excursion vs TP levels.
    by_mfe = 0
    mfe = float(sig.max_favorable_pct or 0.0)
    if mfe > 0 and sig.entry_low and sig.entry_high:
        mid = (float(sig.entry_low) + float(sig.entry_high)) / 2.0
        for level, rank in ((sig.tp1, 1), (sig.tp2, 2), (sig.tp3, 3)):
            if level and mfe >= _favorable_pct_to(float(level), mid, sig.side) > 0:
                by_mfe = max(by_mfe, rank)

    max_tp = max(by_status, by_mfe)
    if max_tp == by_status and by_status > 0 and by_mfe < by_status:
        method = "status"
    elif by_mfe > by_status:
        method = "mfe_recovery"
    elif by_status > 0:
        method = "status"
    else:
        method = "none"
    return max_tp, method


def build_tp_history(sig: Signal) -> tuple[dict, str]:
    """Build/refresh tp_history for a signal. Returns (tp_history, method)."""
    diag = {}
    if sig.diagnostics:
        try:
            parsed = json.loads(sig.diagnostics)
            diag = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            diag = {}
    existing = diag.get("tp_history") if isinstance(diag.get("tp_history"), dict) else {}

    max_tp, method = infer_max_tp_hit(sig, existing)
    status_u = (sig.status or "").upper()

    hist = dict(existing)
    hist["max_tp_hit"] = max_tp
    # Final exit event: prefer recorded one, else derive from status.
    hist.setdefault("final_exit_event", status_u if status_u not in ("OPEN", "") else None)
    if "first_exit_event" not in hist and max_tp == 0 and status_u == "SL":
        hist["first_exit_event"] = "SL"
    hist.setdefault("backfilled", True)

    outcome = classify_trade_outcome(
        status=sig.status,
        realized_pnl=sig.pnl_pct,
        diagnostics={"tp_history": hist},
    )
    hist["trade_outcome"] = outcome.outcome
    return hist, method


async def run(apply: bool, days: int | None) -> None:
    async with SessionLocal() as session:
        q = select(Signal)
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            q = q.where(Signal.created_at >= cutoff)
        rows = await session.execute(q)
        signals = list(rows.scalars().all())

        outcome_counts: Counter = Counter()
        method_counts: Counter = Counter()
        recovered = 0  # legacy SL rows now reclassified as wins
        changes: list[tuple[int, str, str, str]] = []

        for sig in signals:
            hist, method = build_tp_history(sig)
            outcome = hist["trade_outcome"]
            outcome_counts[outcome] += 1
            method_counts[method] += 1
            if (sig.status or "").upper() == "SL" and outcome in (
                "PARTIAL_WIN",
                "WIN",
                "FULL_WIN",
            ):
                recovered += 1
                changes.append((sig.id, sig.symbol, "SL", outcome))

            if apply:
                diag = {}
                if sig.diagnostics:
                    try:
                        diag = json.loads(sig.diagnostics) or {}
                    except (ValueError, TypeError):
                        diag = {}
                diag["tp_history"] = hist
                await session.execute(
                    update(Signal).where(Signal.id == sig.id).values(diagnostics=json.dumps(diag))
                )

        if apply:
            await session.commit()

    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    print("=" * 64)
    print(f"  TRADE OUTCOME BACKFILL — {mode}")
    print("=" * 64)
    print(f"  Signals scanned:        {len(signals)}")
    print(f"  Reclassified SL -> win: {recovered}  (TP-then-SL recovered)")
    print()
    print("  Outcome distribution:")
    for k in ("FULL_WIN", "WIN", "PARTIAL_WIN", "BREAKEVEN", "LOSS", "OPEN"):
        print(f"    {k:<12} {outcome_counts.get(k, 0)}")
    print()
    print("  max_tp_hit inference method:")
    for k, v in method_counts.most_common():
        print(f"    {k:<14} {v}")
    print()
    if changes:
        print("  Sample recovered trades (up to 15):")
        for sid, sym, old, new in changes[:15]:
            print(f"    #{sid} {sym}: {old} -> {new}")
        print()
    if not apply:
        print("  Re-run with --apply to persist tp_history into diagnostics.")
    else:
        print("  ✅ Backfill complete (diagnostics.tp_history written).")
    print("=" * 64)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill lifecycle-aware trade outcomes.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="preview only (default)")
    g.add_argument("--apply", action="store_true", help="persist changes")
    ap.add_argument("--days", type=int, default=None, help="limit to the last N days")
    args = ap.parse_args()

    asyncio.run(run(apply=args.apply, days=args.days))


if __name__ == "__main__":
    main()
