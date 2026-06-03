"""
Signal message templating for Telegram.

Output: clean Markdown that renders well on mobile + desktop and looks like a
premium commercial signal product.
"""

from __future__ import annotations

from app.utils.helpers import fmt_pct, fmt_price

_SIDE_EMOJI = {"LONG": "🚀", "SHORT": "🔻"}
_RISK_EMOJI = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠"}


def _quality_label(conf: float) -> str:
    if conf >= 88:
        return "ELITE"
    if conf >= 80:
        return "HIGH CONFIDENCE"
    if conf >= 72:
        return "QUALITY"
    return "STANDARD"


def _funding_line(sig: dict) -> str:
    """Return a compact funding line if funding data is available in the signal."""
    rate = sig.get("_funding_rate")
    cls = sig.get("_funding_class")
    score = sig.get("_funding_score")
    if rate is None or cls is None:
        return ""
    rate_pct = rate * 100
    label_map = {
        "neutral": "Neutral",
        "positive": "Positive",
        "negative": "Negative / Contrarian",
        "extreme_positive": "Extreme Positive ⚠️",
        "extreme_negative": "Extreme Negative ⚠️",
    }
    label = label_map.get(cls, cls.replace("_", " ").title())
    score_str = f"{score:+d}" if score is not None else ""
    return (
        f"💰 <b>Funding</b> • <code>{rate_pct:.4f}%</code>  {label}  Score <code>{score_str}</code>"
    )


def format_signal(sig: dict) -> str:
    side = sig["side"]
    side_icon = "🟢" if side == "LONG" else "🔴"

    reasons = sig.get("reasons", [])
    if isinstance(reasons, str):
        reasons = [r.strip() for r in reasons.split("|") if r.strip()]

    ai = "\n".join(f"✔ {r}" for r in reasons[:3])
    funding_line = _funding_line(sig)

    body = (
        "⚡ <b>ARGUS QUANT</b>\n\n"
        f"{side_icon} <code>{sig['symbol']}</code> <b>{side}</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        f"<b>ENTRY</b> • "
        f"<code>{fmt_price(sig['entry_low'])} → {fmt_price(sig['entry_high'])}</code>\n"
        f"<b>TARGET</b> • "
        f"<code>{fmt_price(sig['tp1'])}</code> • "
        f"<code>{fmt_price(sig['tp2'])}</code> • "
        f"<code>{fmt_price(sig['tp3'])}</code>\n"
        f"<b>STOP</b> • <code>{fmt_price(sig['stop_loss'])}</code>\n\n"
        f"⚡ <b>RR</b> • <code>1 : {sig['risk_reward']}</code>\n"
        f"📊 <b>CONFIDENCE</b> • <code>{sig['confidence']}%</code>\n\n"
        f"{ai}"
    )
    if funding_line:
        body += f"\n\n{funding_line}"
    return body


def format_event(payload: dict) -> str:
    event = payload["event"]
    if event == "TP1":
        head = "🎯 *TP1 HIT*"
    elif event == "TP2":
        head = "🎯🎯 *TP2 HIT*"
    elif event == "TP3":
        head = "🏆 *TP3 HIT — FULL TARGET*"
    elif event == "SL":
        head = "🛑 *STOP LOSS HIT*"
    else:
        head = "🔔 *UPDATE*"
    pnl = payload.get("pnl_pct", 0.0)
    return f"{head}\n" f"`{payload['symbol']}` · {payload['side']}\n" f"PnL: *{fmt_pct(pnl)}*"


def format_market_overview(data: dict) -> str:
    gainers = data.get("gainers", [])[:5]
    losers = data.get("losers", [])[:5]
    wr = data.get("winrate") or {}

    lines = ["📊 *Market Overview*\n"]
    if gainers:
        lines.append("*Top Gainers (24h):*")
        for g in gainers:
            lines.append(f"• `{g['symbol']}` {fmt_pct(g['change_pct'])}")
        lines.append("")
    if losers:
        lines.append("*Top Losers (24h):*")
        for row in losers:
            lines.append(f"• `{row['symbol']}` {fmt_pct(row['change_pct'])}")
        lines.append("")
    if wr:
        lines.append("*Signal Stats (7d):*")
        lines.append(
            f"• Win rate: `{wr.get('winrate', 0):.1f}%`  "
            f"({wr.get('wins',0)}W / {wr.get('losses',0)}L)"
        )
        lines.append(f"• Avg PnL: `{fmt_pct(wr.get('avg_pnl', 0))}`")
    return "\n".join(lines)
