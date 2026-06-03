"""
Live Pilot — a tiny, gated, Binance-only live run (20–50 USDT).

This is the safest possible on-ramp to real execution: it never runs
automatically and never places an order without ALL of the following:
  * LIVE_PILOT_ENABLED=true,
  * the caller is the single designated LIVE_PILOT_USER_ID,
  * the exact manual confirmation phrase,
  * the request inside the hard pilot limits (Binance-only, BTC/ETH, leverage
    and notional caps, position cap), with a stop-loss AND take-profit attached,
  * the safety layer clear (global kill / user kill / lockout),
  * and finally the live execution gate (otherwise the underlying open runs MOCK).

The actual order goes through app.live_trading.service.open_position, which is
itself gated by live_gate_open(); so with the gate closed this whole path is a
realistic dry-run that places nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import LivePosition
from app.live_trading import service
from app.live_trading.service import LiveTradingError
from app.safety import service as safety
from app.utils.logger import logger

PILOT_EXCHANGE = "binance"
PILOT_CONFIRM_PHRASE = "I UNDERSTAND THIS PLACES A REAL ORDER"


@dataclass
class PilotCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PilotPreflight:
    ok: bool = False
    mode: str = "MOCK"
    checks: list[PilotCheck] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(PilotCheck(name=name, ok=ok, detail=detail))

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks],
        }


def pilot_enabled() -> bool:
    return bool(settings.live_pilot_enabled)


def allowed_symbols() -> list[str]:
    return [s.strip().upper() for s in settings.live_pilot_allowed_symbols.split(",") if s.strip()]


def pilot_config() -> dict:
    return {
        "enabled": pilot_enabled(),
        "exchange": PILOT_EXCHANGE,
        "user_id": settings.live_pilot_user_id,
        "max_notional_usdt": settings.live_pilot_max_notional,
        "max_positions": settings.live_pilot_max_positions,
        "max_leverage": settings.live_pilot_max_leverage,
        "allowed_symbols": allowed_symbols(),
        "require_confirmation": settings.live_pilot_require_confirmation,
        "confirm_phrase": PILOT_CONFIRM_PHRASE,
        **service.gate_status(),
    }


def validate_pilot_request(*, symbol: str, notional_usdt: float, leverage: int) -> list[PilotCheck]:
    """Pure static-limit checks (no DB). Each returned check has ok=True/False."""
    sym = (symbol or "").upper()
    checks: list[PilotCheck] = []
    checks.append(PilotCheck("pilot_enabled", pilot_enabled(), "LIVE_PILOT_ENABLED"))
    checks.append(
        PilotCheck(
            "auto_trading_off",
            not settings.auto_trading_enabled,
            "AUTO_TRADING_ENABLED must stay false",
        )
    )
    checks.append(
        PilotCheck(
            "symbol_allowed",
            sym in allowed_symbols(),
            f"{sym} in {allowed_symbols()}",
        )
    )
    checks.append(
        PilotCheck(
            "leverage_within_cap",
            0 < int(leverage) <= settings.live_pilot_max_leverage,
            f"leverage={leverage} cap={settings.live_pilot_max_leverage}",
        )
    )
    checks.append(
        PilotCheck(
            "notional_within_cap",
            0 < float(notional_usdt) <= settings.live_pilot_max_notional,
            f"notional={notional_usdt} cap={settings.live_pilot_max_notional}",
        )
    )
    return checks


async def _open_position_count(db: AsyncSession, user_id: int) -> int:
    res = await db.execute(
        select(func.count(LivePosition.id)).where(
            LivePosition.user_id == user_id, LivePosition.status == "OPEN"
        )
    )
    return int(res.scalar() or 0)


async def _has_open_symbol(db: AsyncSession, user_id: int, symbol: str) -> bool:
    res = await db.execute(
        select(func.count(LivePosition.id)).where(
            LivePosition.user_id == user_id,
            LivePosition.symbol == symbol.upper(),
            LivePosition.status == "OPEN",
        )
    )
    return int(res.scalar() or 0) > 0


async def pilot_preflight(
    db: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    notional_usdt: float,
    leverage: int,
    has_stop_loss: bool = False,
    has_take_profit: bool = False,
) -> PilotPreflight:
    """Run every pilot safety check and return a structured result. ok=True only
    when every hard check passes."""
    pf = PilotPreflight(mode=service.gate_status().get("mode", "MOCK"))

    # Static limits.
    for c in validate_pilot_request(symbol=symbol, notional_usdt=notional_usdt, leverage=leverage):
        pf.checks.append(c)

    # Single designated pilot user.
    pf.add(
        "designated_pilot_user",
        bool(settings.live_pilot_user_id) and user_id == settings.live_pilot_user_id,
        f"user={user_id} pilot_user={settings.live_pilot_user_id}",
    )

    # Protection must be attached before any live entry.
    pf.add("stop_loss_ready", bool(has_stop_loss), "stop_loss provided")
    pf.add("take_profit_ready", bool(has_take_profit), "take_profit provided")

    # Safety layer: global kill / user kill / timed lockout.
    blocked = await safety.trading_blocked(db, user_id)
    pf.add("safety_clear", blocked is None, blocked or "no global/user kill or lockout")

    # Position cap + no duplicate symbol exposure.
    open_count = await _open_position_count(db, user_id)
    pf.add(
        "position_cap",
        open_count < settings.live_pilot_max_positions,
        f"open={open_count} cap={settings.live_pilot_max_positions}",
    )
    dup = await _has_open_symbol(db, user_id, symbol)
    pf.add(
        "no_existing_symbol_position", not dup, f"{symbol.upper()} already open" if dup else "clear"
    )

    # Best-effort exchange reads (balance/open-orders/reconciliation). These need
    # a connected vault adapter; in MOCK or without one they are advisory and do
    # not fail the preflight.
    try:
        bal = await service.get_balance(db, user_id=user_id, exchange=PILOT_EXCHANGE)
        pf.add(
            "balance_readable",
            True,
            f"{bal.get('available')} {bal.get('asset')} ({bal.get('mode')})",
        )
    except LiveTradingError as exc:
        pf.add("balance_readable", True, f"advisory: {exc.detail}")

    pf.ok = all(c.ok for c in pf.checks)
    return pf


async def pilot_open(
    db: AsyncSession,
    *,
    user_id: int,
    symbol: str,
    side: str,
    notional_usdt: float,
    leverage: int,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    confirm: str,
) -> dict:
    """Manually-confirmed pilot entry. Refuses unless the preflight fully passes;
    delegates the actual order to service.open_position (MOCK unless the live
    gate is open)."""
    if not pilot_enabled():
        raise LiveTradingError(403, "Live pilot is disabled (LIVE_PILOT_ENABLED=false).")
    if settings.live_pilot_require_confirmation and (confirm or "").strip() != PILOT_CONFIRM_PHRASE:
        raise LiveTradingError(
            400, f'Confirmation required: type exactly "{PILOT_CONFIRM_PHRASE}".'
        )
    if stop_loss is None or take_profit is None:
        raise LiveTradingError(400, "Pilot orders require both stop_loss and take_profit.")

    pf = await pilot_preflight(
        db,
        user_id=user_id,
        symbol=symbol,
        notional_usdt=notional_usdt,
        leverage=leverage,
        has_stop_loss=stop_loss is not None,
        has_take_profit=take_profit is not None,
    )
    if not pf.ok:
        failed = [c.name for c in pf.checks if not c.ok]
        raise LiveTradingError(403, f"Pilot preflight failed: {', '.join(failed)}")

    logger.warning(
        f"[pilot] OPEN user={user_id} {symbol} {side} notional={notional_usdt} "
        f"lev={leverage} mode={pf.mode}"
    )
    result = await service.open_position(
        db,
        user_id=user_id,
        exchange=PILOT_EXCHANGE,
        symbol=symbol,
        side=side,
        notional_usdt=notional_usdt,
        leverage=leverage,
        order_type="MARKET",
        take_profit=take_profit,
        stop_loss=stop_loss,
    )
    result["pilot"] = True
    result["preflight"] = pf.to_dict()
    return result


async def pilot_emergency_close(
    db: AsyncSession, *, position_id: int, user_id: int, reason: str, is_admin: bool = False
) -> dict:
    """Reduce-only emergency close for a pilot position (delegates to the
    audited live emergency-close path)."""
    return await service.emergency_close_position(
        db, position_id=position_id, reason=reason, actor_user_id=user_id, is_admin=is_admin
    )
