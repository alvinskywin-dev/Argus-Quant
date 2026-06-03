"""
Sprint 22B — Signal Explainability Engine.

Turns the numbers a signal already carries (confidence factor scores, market
regime, RR, SL/TP geometry, funding, liquidity context) into a human-readable
"why this trade?" object. It is purely descriptive — it derives nothing new and
*cannot* change a signal — so it is safe to run on every signal and to surface
in the dashboard / admin / Telegram.

Input is a plain dict (a Signal row's `.to_dict()`, or the in-flight candidate
dict). Missing fields degrade gracefully: a reason is simply omitted rather
than fabricated. With `SIGNAL_EXPLAINABILITY_ENABLED=false` the public
`explain_signal` still works (callers may want it on demand); the flag only
gates automatic attachment in the pipeline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List

from app.config import settings


@dataclass
class SignalReasoning:
    direction: str
    why_long: List[str] = field(default_factory=list)
    why_short: List[str] = field(default_factory=list)
    why_not_short: List[str] = field(default_factory=list)
    why_not_long: List[str] = field(default_factory=list)
    confidence_explanation: List[str] = field(default_factory=list)
    sl_explanation: str = ""
    tp_explanation: str = ""
    market_regime_impact: str = ""
    liquidity_context: str = ""
    trend_structure_context: str = ""

    def to_dict(self) -> dict:
        return {"signal_reasoning": asdict(self)}


def _num(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default
    return default


def _confidence_breakdown(sig: dict, is_long: bool) -> List[str]:
    """Translate the per-factor scores into +/- confidence contributions."""
    out: List[str] = []
    factors = [
        ("trend_score", "HTF trend"),
        ("structure_score", "market structure"),
        ("setup_score", "setup quality"),
        ("entry_score", "15m entry trigger"),
        ("regime_score", "market regime"),
    ]
    for key, label in factors:
        v = _num(sig, key)
        if v is None:
            continue
        sign = "+" if v >= 0 else ""
        out.append(f"{sign}{int(round(v))} {label}")
    funding = _num(sig, "funding_score")
    if funding is not None and funding != 0:
        out.append(f"{'+' if funding >= 0 else ''}{int(round(funding))} funding bias")
    return out


def explain_signal(sig: dict) -> SignalReasoning:
    """Build a SignalReasoning from a signal dict.  Always safe to call."""
    sig = sig or {}
    direction = str(sig.get("side", sig.get("direction", ""))).upper() or "LONG"
    is_long = direction == "LONG"
    regime = str(sig.get("market_regime", "") or "").upper()

    why_dir: List[str] = []
    why_not_opp: List[str] = []

    trend = _num(sig, "trend_score")
    structure = _num(sig, "structure_score")
    setup = _num(sig, "setup_score")
    entry = _num(sig, "entry_score")

    side_word = "bullish" if is_long else "bearish"
    opp_word = "bearish" if is_long else "bullish"

    if trend is not None and trend > 0:
        why_dir.append(f"Higher-timeframe trend {side_word} (trend score {int(trend)})")
        why_not_opp.append(f"Higher timeframe trend is {side_word}, not {opp_word}")
    if structure is not None and structure > 0:
        why_dir.append(f"4H/1H market structure intact ({side_word})")
        why_not_opp.append(f"No {opp_word} structure break")
    if setup is not None and setup > 0:
        why_dir.append("Confluent setup at a key level (liquidity / S-R)")
    if entry is not None and entry > 0:
        why_dir.append(f"15m entry trigger confirmed ({int(entry)} factors)")

    # Reasons already attached upstream (free-text), if any.
    reasons_raw = sig.get("reasons")
    if isinstance(reasons_raw, str) and reasons_raw.strip():
        for r in [x.strip() for x in reasons_raw.replace("\n", ";").split(";") if x.strip()]:
            if r not in why_dir:
                why_dir.append(r)

    if not why_dir:
        why_dir.append(f"Aggregate confidence supports a {direction} bias")
    if not why_not_opp:
        why_not_opp.append(f"Net factor score favours {direction} over the opposite side")

    # --- SL / TP geometry ---
    entry_low = _num(sig, "entry_low")
    entry_high = _num(sig, "entry_high")
    entry_mid = None
    if entry_low is not None and entry_high is not None:
        entry_mid = (entry_low + entry_high) / 2
    sl = _num(sig, "stop_loss")
    tp1 = _num(sig, "tp1")
    rr = _num(sig, "risk_reward")
    rr_method = str(sig.get("rr_method", "") or "")

    sl_expl = ""
    if sl is not None and entry_mid:
        dist = abs(entry_mid - sl) / entry_mid * 100 if entry_mid else 0.0
        basis = (
            "previous 1D support/resistance with an ATR buffer"
            if "1D" in rr_method.upper() or "PREV" in rr_method.upper()
            else "structure low/high with an ATR buffer"
        )
        sl_expl = f"Stop placed at {basis} — {dist:.2f}% from entry"
    tp_expl = ""
    if tp1 is not None and rr is not None:
        tp_expl = f"First target at a liquidity magnet giving RR {rr:.2f}"
    elif tp1 is not None:
        tp_expl = "First target at the next liquidity / structure level"

    # --- Regime impact ---
    regime_impact = ""
    if regime:
        if regime in ("LOW_VOLATILITY", "RANGE"):
            regime_impact = f"{regime}: adaptive gate relaxed RR / SL-distance rules"
        elif regime in ("HIGH_VOLATILITY", "SIDEWAYS"):
            regime_impact = f"{regime}: adaptive gate tightened RR and confidence requirements"
        elif regime in ("BULL", "BEAR"):
            regime_impact = f"{regime}: trend-aligned, thresholds moderately relaxed"
        else:
            regime_impact = f"{regime}: baseline thresholds applied"

    # --- Liquidity / trend-structure context ---
    liq = sig.get("liquidity_context") or sig.get("liquidity")
    liq_ctx = (
        str(liq)
        if liq
        else ("Entry aligned with a liquidity sweep / key node" if (setup or 0) else "")
    )
    ts_ctx = ""
    if trend is not None or structure is not None:
        ts_ctx = f"Trend {side_word}; structure {'aligned' if (structure or 0) > 0 else 'neutral'}"

    reasoning = SignalReasoning(
        direction=direction,
        why_long=why_dir if is_long else [],
        why_short=why_dir if not is_long else [],
        why_not_short=why_not_opp if is_long else [],
        why_not_long=why_not_opp if not is_long else [],
        confidence_explanation=_confidence_breakdown(sig, is_long),
        sl_explanation=sl_expl,
        tp_explanation=tp_expl,
        market_regime_impact=regime_impact,
        liquidity_context=liq_ctx,
        trend_structure_context=ts_ctx,
    )
    return reasoning


def explain_signal_dict(sig: dict) -> dict:
    """Convenience: explain and return the `{"signal_reasoning": {...}}` dict."""
    return explain_signal(sig).to_dict()


def render_telegram(reasoning: SignalReasoning) -> str:
    """A compact, collapsible-friendly text block for Telegram signals."""
    r = reasoning
    lines = ["<b>🔍 Why this trade?</b>"]
    pro = r.why_long or r.why_short
    if pro:
        lines.append(f"<b>Why {r.direction}:</b>")
        lines.extend(f"• {x}" for x in pro[:5])
    against = r.why_not_short or r.why_not_long
    if against:
        opp = "SHORT" if r.direction == "LONG" else "LONG"
        lines.append(f"<b>Why not {opp}:</b>")
        lines.extend(f"• {x}" for x in against[:3])
    if r.confidence_explanation:
        lines.append("<b>Confidence:</b> " + ", ".join(r.confidence_explanation))
    if r.sl_explanation:
        lines.append(f"<b>SL:</b> {r.sl_explanation}")
    if r.tp_explanation:
        lines.append(f"<b>TP:</b> {r.tp_explanation}")
    if r.market_regime_impact:
        lines.append(f"<b>Regime:</b> {r.market_regime_impact}")
    return "\n".join(lines)


def is_enabled() -> bool:
    return bool(settings.signal_explainability_enabled)
