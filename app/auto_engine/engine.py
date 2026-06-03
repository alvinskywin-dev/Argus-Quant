"""
Sprint 20D — the auto-trading engine (DEMO MODE ONLY).

Flow:  Signal → Risk Check → Position Size → Leverage → Paper Order → SL/TP
       → Position Tracking (break-even / trailing) → Close → Statistics.

Everything executes against per-user PAPER accounts (Sprint 20B). No real
orders are ever placed; LIVE_TRADING_ENABLED is irrelevant here. Invoked from
the app orchestrator (main.py) on new signals and on tracker TP/SL events.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auto_engine import service
from app.auto_engine.risk import evaluate, tighten_stop, trailing_stop
from app.config import settings
from app.database.models import (
    AutoTradeConfig,
    PaperAccount,
    PaperAccountPosition,
    Signal,
)
from app.database.session import get_session
from app.paper_engine import service as paper
from app.utils.logger import logger


def _enabled() -> bool:
    return settings.auto_trade_demo_enabled


# ── new signal → open paper positions for opted-in users ──────────


async def on_new_signal(signal_id: int) -> int:
    """Open demo positions for every eligible user. Returns count opened."""
    if not _enabled():
        return 0
    opened = 0
    try:
        async with get_session() as db:
            signal = await db.get(Signal, signal_id)
            if signal is None:
                return 0
            for user_id, cfg in await _eligible_users(db):
                if await _maybe_open(db, user_id, cfg, signal):
                    opened += 1
    except Exception as exc:  # noqa: BLE001 — never break signal flow
        logger.warning(f"[auto] on_new_signal failed: {exc}")
    if opened:
        logger.info(f"[auto] signal #{signal_id} -> {opened} demo position(s) opened")
    return opened


async def _eligible_users(db: AsyncSession) -> list[tuple[int, AutoTradeConfig]]:
    """Users with auto-trade enabled, OR a paper account with auto_follow on."""
    cfg_rows = (
        (
            await db.execute(
                select(AutoTradeConfig).where(AutoTradeConfig.enabled == True)  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    follow_ids = (
        (
            await db.execute(
                select(PaperAccount.user_id).where(PaperAccount.auto_follow == True)  # noqa: E712
            )
        )
        .scalars()
        .all()
    )

    by_user: dict[int, AutoTradeConfig] = {c.user_id: c for c in cfg_rows}
    for uid in follow_ids:
        if uid not in by_user:
            # auto_follow set but no explicit config -> use defaults, treat as enabled
            cfg = await service.get_or_create_config(db, uid)
            cfg.enabled = True
            by_user[uid] = cfg
    return list(by_user.items())


async def _maybe_open(db: AsyncSession, user_id: int, cfg: AutoTradeConfig, signal: Signal) -> bool:
    if await service.already_executed(db, user_id, signal.id):
        return False

    account = await paper.get_or_create_account(db, user_id)
    summary = await paper.account_summary(db, account)

    # Sprint 20E — safety layer runs FIRST (loss limits, correlation caps,
    # cooldown, loss-streak, kill switches). It can set a timed lockout.
    if settings.safety_layer_enabled:
        from app.safety import service as safety

        sdec = await safety.check(
            db,
            user_id=user_id,
            account=account,
            summary=summary,
            symbol=signal.symbol,
            side=signal.side,
        )
        if not sdec.allow:
            await service.log_execution(
                db,
                user_id=user_id,
                action="SKIP",
                reason=f"safety:{sdec.code}",
                signal_id=signal.id,
                account_id=account.id,
                symbol=signal.symbol,
                detail=sdec.reason,
            )
            return False

    decision = evaluate(
        enabled=cfg.enabled,
        symbol=signal.symbol,
        side=signal.side,
        confidence=float(signal.confidence or 0),
        open_positions=summary["open_positions"],
        available_margin=summary["available_balance"],
        max_positions=cfg.max_positions,
        max_leverage=cfg.max_leverage,
        risk_per_trade_pct=cfg.risk_per_trade_pct,
        allowed_coins=cfg.allowed_coins,
        allowed_exchanges=cfg.allowed_exchanges,
        min_confidence=cfg.min_confidence,
    )
    if not decision.allow:
        await service.log_execution(
            db,
            user_id=user_id,
            action="SKIP",
            reason=decision.reason,
            signal_id=signal.id,
            account_id=account.id,
            symbol=signal.symbol,
        )
        return False

    try:
        pos = await paper.copy_signal(db, account, signal, leverage=decision.leverage)
    except paper.PaperError as exc:
        await service.log_execution(
            db,
            user_id=user_id,
            action="SKIP",
            reason="open_failed",
            signal_id=signal.id,
            account_id=account.id,
            symbol=signal.symbol,
            detail=exc.detail,
        )
        return False

    pos.auto_managed = True
    await service.log_execution(
        db,
        user_id=user_id,
        action="OPEN",
        reason=f"{decision.leverage}x risk={decision.risk_pct}%",
        signal_id=signal.id,
        account_id=account.id,
        position_id=pos.id,
        symbol=signal.symbol,
    )
    return True


# ── tracker TP/SL events → manage auto positions ──────────────────


async def on_signal_event(signal_id: int, event: str, pnl_pct: float = 0.0) -> None:
    """Apply break-even / trailing / close to auto-managed positions."""
    if not _enabled() or event not in ("TP1", "TP2", "TP3", "SL"):
        return
    try:
        async with get_session() as db:
            rows = (
                (
                    await db.execute(
                        select(PaperAccountPosition).where(
                            PaperAccountPosition.signal_id == signal_id,
                            PaperAccountPosition.auto_managed == True,  # noqa: E712
                            PaperAccountPosition.status == "OPEN",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for pos in rows:
                await _manage_position(db, pos, event)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[auto] on_signal_event failed: {exc}")


async def _manage_position(db: AsyncSession, pos: PaperAccountPosition, event: str) -> None:
    account = await db.get(PaperAccount, pos.account_id)
    if account is None:
        return
    cfg = await service.get_or_create_config(db, account.user_id)

    if event in ("TP1", "TP2"):
        # Break-even: move stop to entry once the configured trigger is hit.
        if cfg.use_break_even and event == cfg.break_even_trigger and pos.protection is None:
            pos.stop_loss = pos.entry_price
            pos.protection = "BREAK_EVEN"
            await service.log_execution(
                db,
                user_id=account.user_id,
                action="BREAK_EVEN",
                reason=event,
                signal_id=pos.signal_id,
                account_id=account.id,
                position_id=pos.id,
                symbol=pos.symbol,
            )
        # Trailing: tighten the stop behind the just-hit target.
        if cfg.use_trailing_stop:
            ref = pos.tp1 if event == "TP1" else pos.tp2
            if ref:
                candidate = trailing_stop(pos.side, ref, cfg.trailing_distance_pct)
                pos.stop_loss = tighten_stop(pos.side, pos.stop_loss or 0.0, candidate)
                pos.protection = "TRAILING"
                await service.log_execution(
                    db,
                    user_id=account.user_id,
                    action="TRAIL",
                    reason=event,
                    signal_id=pos.signal_id,
                    account_id=account.id,
                    position_id=pos.id,
                    symbol=pos.symbol,
                    detail=f"stop={pos.stop_loss:.6f}",
                )
        return

    # TP3 / SL -> close. SL closes at the (possibly break-even/trailed) stop.
    if event == "TP3":
        exit_price = float(pos.tp3 or pos.entry_price)
    else:  # SL
        exit_price = float(pos.stop_loss or pos.entry_price)

    trade = await paper.close_position(
        db,
        account,
        pos.id,
        mark=exit_price,
        reason=event,
    )
    await service.log_execution(
        db,
        user_id=account.user_id,
        action="CLOSE",
        reason=event,
        signal_id=pos.signal_id,
        account_id=account.id,
        position_id=pos.id,
        symbol=pos.symbol,
        detail=f"pnl={trade.pnl_usdt:.2f}",
    )


# ── statistics ────────────────────────────────────────────────────


async def status(db: AsyncSession, user_id: int) -> dict:
    cfg = await service.get_or_create_config(db, user_id)
    account = await paper.get_or_create_account(db, user_id)
    open_auto = (
        (
            await db.execute(
                select(PaperAccountPosition).where(
                    PaperAccountPosition.account_id == account.id,
                    PaperAccountPosition.auto_managed == True,  # noqa: E712
                    PaperAccountPosition.status == "OPEN",
                )
            )
        )
        .scalars()
        .all()
    )
    execs = await service.list_executions(db, user_id, limit=10_000)
    opened = sum(1 for e in execs if e.action == "OPEN")
    closed = sum(1 for e in execs if e.action == "CLOSE")
    skipped = sum(1 for e in execs if e.action == "SKIP")
    return {
        "enabled": cfg.enabled or account.auto_follow,
        "global_demo_enabled": settings.auto_trade_demo_enabled,
        "open_auto_positions": len(open_auto),
        "total_opened": opened,
        "total_closed": closed,
        "total_skipped": skipped,
    }
