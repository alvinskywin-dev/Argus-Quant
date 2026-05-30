"""
Reporting engine — daily, weekly, monthly Telegram reports.

Reports are formatted as Telegram HTML messages and sent to admin chat(s).
They include: signals generated, win/loss, win rate, best/worst symbols,
performance summary, and open positions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select, desc

from app.config import settings
from app.database.models import Signal, DailyStat
from app.database.session import SessionLocal
from app.utils.logger import logger


def _pct(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


async def _fetch_signals(since: datetime, until: Optional[datetime] = None) -> List[Signal]:
    async with SessionLocal() as session:
        q = select(Signal).where(Signal.created_at >= since)
        if until:
            q = q.where(Signal.created_at < until)
        res = await session.execute(q.order_by(desc(Signal.created_at)).limit(5000))
        return res.scalars().all()


def _build_stats(signals: List[Signal]) -> dict:
    closed = [s for s in signals if s.status in ("TP1", "TP2", "TP3", "SL")]
    open_s = [s for s in signals if s.status == "OPEN"]
    wins = [s for s in closed if s.status in ("TP1", "TP2", "TP3")]
    losses = [s for s in closed if s.status == "SL"]
    pnls = [float(s.pnl_pct or 0) for s in closed]
    win_rate = round(len(wins) / max(1, len(closed)) * 100, 1)
    avg_pnl = round(sum(pnls) / max(1, len(pnls)), 2) if pnls else 0.0
    best = round(max(pnls, default=0.0), 2)
    worst = round(min(pnls, default=0.0), 2)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = round(gross_win / max(0.001, gross_loss), 2)

    # Symbol leaderboard
    sym_map: dict = {}
    for s in closed:
        sym_map.setdefault(s.symbol, []).append(float(s.pnl_pct or 0))
    leaderboard = sorted(
        [(k, round(sum(v) / len(v), 2), len(v)) for k, v in sym_map.items()],
        key=lambda x: x[1], reverse=True,
    )

    return {
        "total": len(signals),
        "closed": len(closed),
        "open": len(open_s),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "best": best,
        "worst": worst,
        "profit_factor": pf,
        "leaderboard": leaderboard,
    }


class DailyReport:
    """Generate and send daily performance report."""

    async def generate(self, date: Optional[datetime] = None) -> str:
        if date is None:
            date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        since = date
        until = date + timedelta(days=1)
        signals = await _fetch_signals(since, until)
        st = _build_stats(signals)
        date_str = date.strftime("%Y-%m-%d")

        lines = [
            f"📊 <b>DAILY REPORT — {date_str}</b>",
            "",
            f"Signals:  <b>{st['total']}</b> total  ({st['open']} open)",
            f"Closed:   <b>{st['closed']}</b>  |  ✅ {st['wins']}W  ❌ {st['losses']}L",
            f"Win Rate: <b>{st['win_rate']}%</b>",
            f"Avg PnL:  <b>{_pct(st['avg_pnl'])}</b>",
            f"Best:     <b>{_pct(st['best'])}</b>",
            f"Worst:    <b>{_pct(st['worst'])}</b>",
            f"P. Factor:<b>{st['profit_factor']}</b>",
        ]
        if st["leaderboard"]:
            lines += ["", "🏆 <b>Top Symbols:</b>"]
            for sym, avg, cnt in st["leaderboard"][:5]:
                lines.append(f"  • <code>{sym}</code>  {_pct(avg)}  ({cnt} trades)")

        return "\n".join(lines)

    async def send(self, bot, date: Optional[datetime] = None) -> None:
        try:
            text = await self.generate(date)
            await bot.alert_admin("Daily Report", text, parse_mode="HTML")
            logger.info("📊 Daily report sent")
        except Exception as exc:
            logger.exception(f"Daily report failed: {exc}")


class WeeklyReport:
    """Generate and send weekly performance report."""

    async def generate(self, week_start: Optional[datetime] = None) -> str:
        if week_start is None:
            now = datetime.now(timezone.utc)
            week_start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        since = week_start
        until = week_start + timedelta(days=7)
        signals = await _fetch_signals(since, until)
        st = _build_stats(signals)
        week_str = f"{since.strftime('%b %d')} – {(until - timedelta(days=1)).strftime('%b %d, %Y')}"

        lines = [
            f"📈 <b>WEEKLY REPORT</b>",
            f"<i>{week_str}</i>",
            "",
            f"Total Signals: <b>{st['total']}</b>",
            f"Closed:  ✅ <b>{st['wins']}</b> wins  ❌ <b>{st['losses']}</b> losses",
            f"Win Rate: <b>{st['win_rate']}%</b>",
            f"Avg PnL:  <b>{_pct(st['avg_pnl'])}</b>",
            f"Profit Factor: <b>{st['profit_factor']}</b>",
            f"Best Trade: <b>{_pct(st['best'])}</b>",
            f"Worst Trade: <b>{_pct(st['worst'])}</b>",
        ]
        if st["leaderboard"]:
            lines += ["", "🏆 <b>Best Symbols:</b>"]
            for sym, avg, cnt in st["leaderboard"][:8]:
                lines.append(f"  • <code>{sym}</code>  {_pct(avg)}  ({cnt})")
            worst = sorted(st["leaderboard"], key=lambda x: x[1])
            if worst and worst[0][1] < 0:
                lines += ["", "⚠️ <b>Worst Symbols:</b>"]
                for sym, avg, cnt in worst[:3]:
                    if avg < 0:
                        lines.append(f"  • <code>{sym}</code>  {_pct(avg)}  ({cnt})")

        return "\n".join(lines)

    async def send(self, bot, week_start: Optional[datetime] = None) -> None:
        try:
            text = await self.generate(week_start)
            await bot.alert_admin("Weekly Report", text, parse_mode="HTML")
            logger.info("📈 Weekly report sent")
        except Exception as exc:
            logger.exception(f"Weekly report failed: {exc}")


class MonthlyReport:
    """Generate and send monthly performance report."""

    async def generate(self, month: Optional[datetime] = None) -> str:
        if month is None:
            now = datetime.now(timezone.utc)
            month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        since = month
        if month.month == 12:
            until = month.replace(year=month.year + 1, month=1)
        else:
            until = month.replace(month=month.month + 1)
        signals = await _fetch_signals(since, until)
        st = _build_stats(signals)
        month_str = month.strftime("%B %Y")

        lines = [
            f"📅 <b>MONTHLY REPORT — {month_str}</b>",
            "",
            f"Total Signals: <b>{st['total']}</b>",
            f"Closed: <b>{st['closed']}</b>  ({st['open']} still open)",
            f"Results: ✅ <b>{st['wins']}</b> wins  ❌ <b>{st['losses']}</b> losses",
            f"Win Rate: <b>{st['win_rate']}%</b>",
            f"Avg PnL: <b>{_pct(st['avg_pnl'])}</b>",
            f"Profit Factor: <b>{st['profit_factor']}</b>",
            f"Best Trade:  <b>{_pct(st['best'])}</b>",
            f"Worst Trade: <b>{_pct(st['worst'])}</b>",
        ]
        if st["leaderboard"]:
            lines += ["", "🏆 <b>Symbol Leaderboard (by avg PnL):</b>"]
            for i, (sym, avg, cnt) in enumerate(st["leaderboard"][:10], 1):
                lines.append(f"  {i}. <code>{sym}</code>  {_pct(avg)}  ({cnt} trades)")

        return "\n".join(lines)

    async def send(self, bot, month: Optional[datetime] = None) -> None:
        try:
            text = await self.generate(month)
            await bot.alert_admin("Monthly Report", text, parse_mode="HTML")
            logger.info("📅 Monthly report sent")
        except Exception as exc:
            logger.exception(f"Monthly report failed: {exc}")
