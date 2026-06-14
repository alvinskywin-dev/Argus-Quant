"""public router — extracted from server.py (Phase 4).

Handlers moved verbatim; shared helpers/views/templates are imported
from app.dashboard.server. Wired via create_app().include_router().
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import desc, select

from app.analytics.trade_outcome import outcome_for_signal
from app.config import settings
from app.dashboard.routes.analytics_router import api_public_market_regime, api_public_performance
from app.dashboard.routes.system_router import status_route
from app.dashboard.server import (
    _DASH_TTL,
    _MTF_STRATEGY,
    _MTF_TIMEFRAMES,
    _PUBLIC_HTML,
    _backtest_page_html,
    _dash_cache,
    _esc,
    _get_stats,
    _info_page,
    _json_body,
    _market_radar_page_html,
    _performance_center_page_html,
    _performance_page_html,
    _safe_url,
    _safe_wallet,
    _setup_library_page_html,
    _signal_detail_page_html,
    _signals_page_html,
    _stats_page_html,
)
from app.database.models import AffiliateClick, Signal
from app.database.session import SessionLocal
from app.market_data.ws_engine import ws_health

router = APIRouter()


@router.get("/api/public/stats")
async def api_public_stats():
    try:
        return JSONResponse(await _get_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/dashboard")
async def api_public_dashboard():
    now = time.time()
    if _dash_cache["data"] is not None and now - _dash_cache["ts"] < _DASH_TTL:
        return JSONResponse(_dash_cache["data"])

    result = {
        "stats": {},
        "signals": [],
        "positions": [],
        "health": {},
        "performance": {},
        "market_regime": {},
    }

    try:
        result["stats"] = await _get_stats() or {}
    except Exception:
        result["stats"] = {}

    try:
        sigs = await _json_body(await api_public_signals(limit=100))
        result["signals"] = sigs if isinstance(sigs, list) else []
    except Exception:
        result["signals"] = []

    # open positions = open signals (public proxy), normalised to signal shape
    result["positions"] = [
        s
        for s in result["signals"]
        if isinstance(s, dict) and str(s.get("status", "")).upper() == "OPEN"
    ][:20]

    try:
        result["health"] = await status_route() or {}
    except Exception:
        result["health"] = {}

    try:
        perf = await _json_body(await api_public_performance())
        result["performance"] = perf if isinstance(perf, dict) and "error" not in perf else {}
    except Exception:
        result["performance"] = {}

    try:
        mr = await _json_body(await api_public_market_regime())
        result["market_regime"] = mr if isinstance(mr, dict) and "error" not in mr else {}
    except Exception:
        result["market_regime"] = {}

    _dash_cache["data"] = result
    _dash_cache["ts"] = now
    return JSONResponse(result)


@router.get("/api/public/signals")
async def api_public_signals(limit: int = 50):
    limit = min(max(1, limit), 200)
    try:
        async with SessionLocal() as session:
            res = await session.execute(
                select(Signal)
                .where(
                    Signal.strategy == _MTF_STRATEGY,
                    Signal.timeframe.in_(_MTF_TIMEFRAMES),
                )
                .order_by(desc(Signal.created_at))
                .limit(limit)
            )
            rows = res.scalars().all()
        return JSONResponse(
            [
                {
                    "id": s.id,
                    "symbol": s.symbol,
                    "side": s.side,
                    "timeframe": s.timeframe,
                    "confidence": round(float(s.confidence or 0), 1),
                    "risk_reward": round(float(s.risk_reward or 0), 2),
                    "status": s.status,
                    "pnl_pct": round(float(s.pnl_pct or 0), 2),
                    "entry_low": round(float(s.entry_low or 0), 6),
                    "tp1": round(float(s.tp1 or 0), 6),
                    "sl": round(float(s.stop_loss or 0), 6),
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in rows
            ]
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/prices")
async def api_public_prices():
    return JSONResponse(ws_health())


@router.get("/api/public/signal/{signal_id}")
async def api_public_signal(signal_id: int):
    try:
        async with SessionLocal() as session:
            sig = await session.get(Signal, signal_id)
        if sig is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        reasons_list = [r.strip() for r in (sig.reasons or "").split("|") if r.strip()]
        # Lifecycle-aware outcome so a TP-then-SL trade renders as a win, not a loss.
        outcome = outcome_for_signal(sig)
        return JSONResponse(
            {
                "id": sig.id,
                "symbol": sig.symbol,
                "side": sig.side,
                "timeframe": sig.timeframe,
                "confidence": round(float(sig.confidence or 0), 1),
                "risk_reward": round(float(sig.risk_reward or 0), 2),
                "risk_level": sig.risk_level or "",
                "strategy": sig.strategy or "",
                "status": sig.status,
                "trade_outcome": outcome.outcome,
                "winrate_bucket": outcome.winrate_bucket,
                "max_tp_hit": outcome.max_tp_hit,
                "pnl_pct": round(float(sig.pnl_pct or 0), 2),
                "entry_low": round(float(sig.entry_low or 0), 6),
                "entry_high": round(float(sig.entry_high or 0), 6),
                "tp1": round(float(sig.tp1 or 0), 6),
                "tp2": round(float(sig.tp2 or 0), 6),
                "tp3": round(float(sig.tp3 or 0), 6),
                "stop_loss": round(float(sig.stop_loss or 0), 6),
                "trend_score": sig.trend_score,
                "structure_score": sig.structure_score,
                "setup_score": sig.setup_score,
                "entry_score": sig.entry_score,
                "reasons": reasons_list,
                "created_at": sig.created_at.isoformat() if sig.created_at else None,
                "closed_at": sig.closed_at.isoformat() if sig.closed_at else None,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/diagnostics/{signal_id}")
async def api_public_diagnostics(signal_id: int):
    """
    Sprint 16A — Return full diagnostics for a signal.
    Includes per-layer scores, funding/OI/liquidity scores, and RR method.
    """
    import json as _json

    try:
        async with SessionLocal() as session:
            sig = await session.get(Signal, signal_id)
        if sig is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        diag: dict = {}
        if sig.diagnostics:
            try:
                diag = _json.loads(sig.diagnostics)
            except Exception:
                diag = {}
        return JSONResponse(
            {
                "signal_id": signal_id,
                "symbol": sig.symbol,
                "side": sig.side,
                "confidence": round(float(sig.confidence or 0), 1),
                "rr_method": sig.rr_method or "atr",
                "diagnostics": diag,
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/public/languages")
async def api_public_languages():
    """List of all supported UI languages with code and native name."""
    from app.dashboard.i18n import SUPPORTED_LANGUAGES

    return JSONResponse({"languages": SUPPORTED_LANGUAGES, "count": len(SUPPORTED_LANGUAGES)})


@router.get("/api/public/translations")
async def api_public_translations(lang: str = "en"):
    """Lazy-load the full translation map for *lang*. Falls back to English."""
    import re as _re

    safe = _re.sub(r"[^a-zA-Z]", "", lang)[:8].lower() or "en"
    from app.dashboard.i18n import load_locale

    return JSONResponse(load_locale(safe))


@router.get("/api/public/strategy")
async def api_public_strategy():
    """Public strategy engine config — exact filters and logic descriptions powering the bot."""
    entry_pass = int(os.getenv("ENTRY_PASS_SCORE", str(settings.entry_pass_score)))
    cooldown_min = round(settings.signal_cooldown_sec / 60, 1)
    return JSONResponse(
        {
            "timeframes": {
                "trend": "1D",
                "structure": "4H",
                "setup": "1H",
                "entry": "15M",
            },
            "filters": {
                "min_confidence": settings.min_confidence,
                "min_rr": settings.min_rr,
                "entry_pass_score": entry_pass,
                "max_signals_per_hour": settings.max_signals_per_hour,
                "cooldown_seconds": settings.signal_cooldown_sec,
            },
            "strategy": {
                "trend_engine": (
                    "EMA50 vs EMA200 cross required. LONG: EMA50 > EMA200. "
                    "SHORT: EMA50 < EMA200. BOS/MSS market structure confirmation. "
                    "Score: 10 base + EMA separation bonus (max 5) + structure bonus (5) = max 20 pts."
                ),
                "structure_engine": (
                    "4H confluence: BOS · CHoCH · Order Block · Fair Value Gap · Liquidity Sweep. "
                    "Minimum 2/5 hits required. Each extra hit adds confidence bonus."
                ),
                "setup_engine": (
                    "1H conditions: Pullback zone · Retest · VWAP alignment · EMA alignment · Volume spike. "
                    "Minimum 3/5 conditions required. Extra hits add confidence bonus."
                ),
                "entry_engine": (
                    f"15M factors (each 1 pt, max 5): BOS · FVG retest · OB retest · "
                    f"EMA pullback · VWAP reclaim. Minimum {entry_pass}/5 required to trigger entry."
                ),
                "funding_engine": (
                    "Funding rate classification: Neutral (|rate| < 0.03%), "
                    "Crowded Long (rate > 0.08%), Crowded Short (rate < -0.08%). "
                    "Extreme funding blocks signal emission. Filter weight: 10 pts."
                ),
                "risk_engine": (
                    f"Confidence gate: ≥ {settings.min_confidence}%. "
                    f"RR gate: ≥ 1:{settings.min_rr}. "
                    f"Rate limiter: max {settings.max_signals_per_hour}/hr. "
                    f"Cooldown: {cooldown_min} min per symbol+side."
                ),
            },
        }
    )


@router.get("/aff/{exchange}")
async def affiliate_redirect(exchange: str, request: Request):
    """Track affiliate click then redirect to the affiliate URL."""
    exchange = exchange.lower().strip()
    url_map = {
        "binance": settings.binance_affiliate_url,
        "bybit": settings.bybit_affiliate_url,
        "okx": settings.okx_affiliate_url,
        "bitget": settings.bitget_affiliate_url,
    }
    dest = _safe_url(url_map.get(exchange, ""))
    if not dest:
        return RedirectResponse("/", status_code=302)

    # Record click
    try:
        async with SessionLocal() as session:
            click = AffiliateClick(
                exchange=exchange,
                referrer=request.headers.get("referer", "")[:256],
            )
            session.add(click)
            await session.commit()
    except Exception:
        pass

    return RedirectResponse(dest, status_code=302)


@router.get("/", response_class=HTMLResponse)
async def index():
    tg_url = _safe_url(settings.telegram_channel_url or os.getenv("TELEGRAM_CHANNEL_URL", ""))
    dc_url = _safe_url(settings.discord_url or os.getenv("DISCORD_URL", ""))
    trc20 = _safe_wallet(settings.donate_usdt_trc20 or os.getenv("DONATE_USDT_TRC20", ""))
    bep20 = _safe_wallet(settings.donate_usdt_bep20 or os.getenv("DONATE_USDT_BEP20", ""))
    btc_addr = _safe_wallet(settings.donate_btc or os.getenv("DONATE_BTC", ""))
    eth_addr = _safe_wallet(settings.donate_eth or os.getenv("DONATE_ETH", ""))
    binance_aff = _safe_url(
        settings.binance_affiliate_url or os.getenv("BINANCE_AFFILIATE_URL", "")
    )
    bybit_aff = _safe_url(settings.bybit_affiliate_url or os.getenv("BYBIT_AFFILIATE_URL", ""))
    okx_aff = _safe_url(settings.okx_affiliate_url or os.getenv("OKX_AFFILIATE_URL", ""))
    bitget_aff = _safe_url(settings.bitget_affiliate_url or os.getenv("BITGET_AFFILIATE_URL", ""))

    html = _PUBLIC_HTML

    # ── nav buttons ─────────────────────────────────────────────────
    tg_btn = (
        f'<a href="{tg_url}" target="_blank" rel="noopener" class="nav-tg">Join Telegram</a>'
        if tg_url
        else ""
    )
    dc_btn = (
        f'<a href="{dc_url}" target="_blank" rel="noopener" class="nav-dc">Discord</a>'
        if dc_url
        else ""
    )
    html = html.replace("__TG_BTN__", tg_btn).replace("__DC_BTN__", dc_btn)

    # ── hero CTA buttons ────────────────────────────────────────────
    hero_btns = []
    if tg_url:
        hero_btns.append(
            f'<a href="{tg_url}" target="_blank" rel="noopener" class="btn-primary">'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.96 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>'
            f"Join Telegram Channel</a>"
        )
    if dc_url:
        hero_btns.append(
            f'<a href="{dc_url}" target="_blank" rel="noopener" class="btn-outline">'
            f"Join Discord</a>"
        )
    hero_btns.append('<a href="/performance" class="btn-outline">&#128200; View Performance</a>')
    html = html.replace("__HERO_BTNS__", "".join(hero_btns))

    # ── raw URLs for inline CTA / float button / footer ─────────────
    html = html.replace("__TG_URL__", tg_url or "#")
    html = html.replace("__DC_URL__", dc_url or "#")

    # ── footer community links ───────────────────────────────────────
    twitter_url = _safe_url(os.getenv("TWITTER_URL", ""))
    youtube_url = _safe_url(os.getenv("YOUTUBE_URL", ""))
    footer_comm = []
    if tg_url:
        footer_comm.append(f'<a href="{tg_url}" target="_blank" rel="noopener">Telegram</a>')
    if dc_url:
        footer_comm.append(f'<a href="{dc_url}" target="_blank" rel="noopener">Discord</a>')
    if twitter_url:
        footer_comm.append(
            f'<a href="{twitter_url}" target="_blank" rel="noopener">Twitter / X</a>'
        )
    if youtube_url:
        footer_comm.append(f'<a href="{youtube_url}" target="_blank" rel="noopener">YouTube</a>')
    html = html.replace("__FOOTER_COMM__", "".join(footer_comm))

    # ── donation wallets ─────────────────────────────────────────────
    don_cards = []
    wallets = [
        ("USDT", "TRC20", "Tron Network", trc20, "#26a17b"),
        ("USDT", "BEP20", "BSC Network", bep20, "#f3ba2f"),
        ("BTC", "BTC", "Bitcoin", btc_addr, "#f7931a"),
        ("ETH", "ETH", "Ethereum", eth_addr, "#627eea"),
    ]
    for coin, net, netname, addr, color in wallets:
        if addr:
            safe_coin = _esc(coin)
            safe_net = _esc(net)
            safe_netname = _esc(netname)
            don_cards.append(
                f'<div class="don-card card">'
                f'<div class="don-hdr">'
                f'<span class="don-coin" style="color:{color}">{safe_coin} &mdash; {safe_net}</span>'
                f'<span class="don-net">{safe_netname}</span>'
                f"</div>"
                f'<div class="don-addr">{addr}</div>'
                f'<div class="don-acts">'
                f'<button class="don-btn don-copy" onclick="copyDonAddr(this,\'{addr}\')">Copy</button>'
                f"<button class=\"don-btn don-qr\" onclick=\"showQR('{safe_coin}','{safe_net} &mdash; {safe_netname}','{addr}')\">QR Code</button>"
                f"</div></div>"
            )
    if not don_cards:
        for coin, net, netname, _addr, color in wallets:
            don_cards.append(
                f'<div class="don-card card disabled">'
                f'<div class="don-hdr"><span class="don-coin" style="color:{color}">{_esc(coin)} &mdash; {_esc(net)}</span>'
                f'<span class="don-net">{_esc(netname)}</span></div>'
                f'<div class="don-empty">Wallet address not configured yet</div>'
                f"</div>"
            )
    donate_section = (
        '<div class="sh">'
        '<div class="sh-lbl">&#9829; SUPPORT</div>'
        '<div class="sh-title">Support Argus Quant &#10084;&#65039;</div>'
        '<div class="sh-sub">If Argus Quant helps you, consider supporting development and server costs.</div>'
        "</div>"
        '<div class="don-intro">'
        '<div class="card" style="padding:20px">'
        '<div style="font-weight:900;font-size:18px;margin-bottom:8px;color:var(--text)">Keep Argus Quant Free</div>'
        '<div class="don-copyline">Every donation helps fund data, servers, backtesting, and new features for the trading community.</div>'
        '<div style="margin-top:14px;color:var(--green);font-size:12px;font-weight:800">Thank you for your support! &#128591;</div>'
        "</div>"
        '<div class="don-grid">' + "".join(don_cards) + "</div>"
        "</div>"
    )
    html = html.replace("__DONATE__", donate_section)

    # ── exchange affiliate cards ─────────────────────────────────────
    aff_cards = []
    exchanges = [
        (
            "Binance",
            binance_aff,
            "#f3ba2f",
            "binance",
            "Best for Binance",
            "World's largest crypto exchange with deepest liquidity.",
        ),
        (
            "Bybit",
            bybit_aff,
            "#f7a600",
            "bybit",
            "Best Futures Platform",
            "Top derivatives & perpetual futures with low fees.",
        ),
        (
            "OKX",
            okx_aff,
            "#1a82ff",
            "okx",
            "Leading Altcoins",
            "Advanced trading tools and deep altcoin markets.",
        ),
        (
            "Bitget",
            bitget_aff,
            "#00e6b3",
            "bitget",
            "Best Copy Trading",
            "Follow top traders automatically with copy trading.",
        ),
    ]
    for name, url, color, logo, tag, descr in exchanges:
        safe_name = _esc(name)
        if url:
            btn = (
                f'<a href="{url}" target="_blank" rel="noopener" class="exch-btn" '
                f'style="background:{color};box-shadow:0 4px 14px {color}44">Register Now &rarr;</a>'
            )
            disabled_cls = ""
        else:
            btn = '<span class="exch-btn coming-soon">Coming Soon</span>'
            disabled_cls = " disabled"
        aff_cards.append(
            f'<div class="exch-card card{disabled_cls}">'
            f'<div class="exch-ico"><img src="/static/exchanges/{logo}.svg" alt="{safe_name}" class="exch-logo-img"></div>'
            f'<div class="exch-name" style="color:{color}">{safe_name}</div>'
            f'<div class="exch-tag">{tag}</div>'
            f'<div class="exch-desc">{descr}</div>'
            f"{btn}"
            f"</div>"
        )
    aff_section = '<div class="exch-grid">' + "".join(aff_cards) + "</div>"
    html = html.replace("__AFFILIATES__", aff_section)

    return HTMLResponse(html)


@router.get("/signal/{signal_id}", response_class=HTMLResponse)
async def signal_detail_page(signal_id: int):
    return HTMLResponse(_signal_detail_page_html(signal_id))


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page():
    return HTMLResponse(_backtest_page_html())


@router.get("/signals", response_class=HTMLResponse)
async def signals_page():
    return HTMLResponse(_signals_page_html())


@router.get("/performance", response_class=HTMLResponse)
async def performance_page():
    return HTMLResponse(_performance_page_html())


@router.get("/stats", response_class=HTMLResponse)
async def stats_page():
    return HTMLResponse(_stats_page_html())


@router.get("/performance-center", response_class=HTMLResponse)
async def performance_center_page():
    return HTMLResponse(_performance_center_page_html())


@router.get("/market-radar", response_class=HTMLResponse)
async def market_radar_page():
    return HTMLResponse(_market_radar_page_html())


@router.get("/setup-library", response_class=HTMLResponse)
async def setup_library_page():
    return HTMLResponse(_setup_library_page_html())


@router.get("/about", response_class=HTMLResponse)
async def about_page():
    return HTMLResponse(
        _info_page(
            "About",
            """
<h2>About ARGUS QUANT</h2>
<p>ARGUS QUANT is a free, AI-powered crypto futures signal service. Our multi-timeframe analysis engine scans the market 24/7 and delivers high-quality trade setups directly to Telegram.</p>
<h3>How It Works</h3>
<ul style="color:#c9d8e8;line-height:2">
  <li><strong>1D Trend Filter</strong> — Identifies the dominant daily trend using EMA and market structure.</li>
  <li><strong>4H Structure</strong> — Confirms Break of Structure, Order Blocks, and Fair Value Gaps.</li>
  <li><strong>1H Setup</strong> — Detects pullbacks, retests, VWAP alignment, and volume confirmation.</li>
  <li><strong>15M Entry</strong> — Triggers on momentum breakout with score-based confirmation.</li>
</ul>
<h3>Signal Tiers</h3>
<ul style="color:#c9d8e8;line-height:2">
  <li><strong>ELITE (95-100%)</strong> — Highest confidence setups.</li>
  <li><strong>VIP (85-94%)</strong> — Strong setups with solid confluence.</li>
  <li><strong>PUBLIC (75-84%)</strong> — Good setups meeting minimum criteria.</li>
</ul>
<h3>Free Forever</h3>
<p>All signals are provided free of charge. We monetize through voluntary donations and affiliate partnerships.</p>
<p><a href="/faq">Frequently Asked Questions →</a></p>
""",
        )
    )


@router.get("/faq", response_class=HTMLResponse)
async def faq_page():
    return HTMLResponse(
        _info_page(
            "FAQ",
            """
<h2>Frequently Asked Questions</h2>

<h3>Are the signals free?</h3>
<p>Yes. All signals on ARGUS QUANT are 100% free. No subscription or payment required.</p>

<h3>How do I receive signals?</h3>
<p>Join our Telegram channel. Signals are posted automatically as soon as the AI engine detects a valid setup.</p>

<h3>What markets do you cover?</h3>
<p>We scan USDT perpetual futures on Binance — all liquid pairs with >$5M daily volume.</p>

<h3>How accurate are the signals?</h3>
<p>Accuracy varies by market conditions. Check the live performance statistics on our dashboard. Past results do not guarantee future performance.</p>

<h3>What does confidence % mean?</h3>
<p>Confidence is our AI engine's assessment of signal quality (75-100%). Higher = more confluences aligned. It is not a win probability.</p>

<h3>What is Risk/Reward (RR)?</h3>
<p>RR is the ratio of potential profit to potential loss. A 1:2.5 RR means you can gain $2.50 for every $1 risked. We require a minimum of 1:2.0.</p>

<h3>Should I use all my capital on one signal?</h3>
<p>No. Never risk more than 1-2% of your trading capital on a single trade. Proper position sizing is essential for long-term survival.</p>

<h3>Can I auto-trade these signals?</h3>
<p>Auto-trading is on our roadmap but not currently available. All signals require manual execution.</p>

<h3>Who runs this project?</h3>
<p>ARGUS QUANT is an independent trading tools project. We are not a registered financial institution.</p>
""",
        )
    )


@router.get("/terms", response_class=HTMLResponse)
async def terms():
    return HTMLResponse(
        _info_page(
            "Terms of Service",
            """
<h2>Terms of Service</h2>
<p>Last updated: 2026-05-30</p>
<h3>1. Acceptance</h3>
<p>By using ARGUS QUANT ("the Service") you agree to these Terms. If you do not agree, stop using the Service immediately.</p>
<h3>2. Educational Purpose Only</h3>
<p>All signals, analysis, and content provided by the Service are for educational and informational purposes only. Nothing on this platform constitutes financial, investment, trading, or legal advice.</p>
<h3>3. No Guarantees</h3>
<p>Past performance is not indicative of future results. Signal accuracy cannot be guaranteed. You may lose all of your capital.</p>
<h3>4. User Responsibility</h3>
<p>You are solely responsible for your trading decisions. Always conduct your own research and consult a qualified financial advisor before making any investment.</p>
<h3>5. Limitation of Liability</h3>
<p>ARGUS QUANT and its operators shall not be liable for any losses, damages, or costs arising from your use of the Service.</p>
<h3>6. Modifications</h3>
<p>We reserve the right to modify these Terms at any time. Continued use of the Service constitutes acceptance of the updated Terms.</p>
""",
        )
    )


@router.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return HTMLResponse(
        _info_page(
            "Privacy Policy",
            """
<h2>Privacy Policy</h2>
<p>Last updated: 2026-05-30</p>
<h3>1. Data We Collect</h3>
<p>The public dashboard does not require account creation. We do not collect personally identifiable information from visitors to the public site.</p>
<p>If you join our Telegram channel, Telegram's own privacy policy governs the processing of your Telegram data.</p>
<h3>2. Server Logs</h3>
<p>Our servers may log IP addresses and request metadata for security monitoring and abuse prevention. These logs are retained for up to 30 days.</p>
<h3>3. Cookies</h3>
<p>The admin dashboard uses a session cookie strictly for authentication. No tracking or advertising cookies are used.</p>
<h3>4. Affiliate Links</h3>
<p>Clicking an affiliate link may set cookies on the destination exchange's website. We do not control third-party cookies.</p>
<h3>5. Contact</h3>
<p>For privacy-related inquiries please reach out via our Telegram channel.</p>
""",
        )
    )


@router.get("/risk-disclaimer", response_class=HTMLResponse)
async def risk_disclaimer():
    return HTMLResponse(
        _info_page(
            "Risk Disclaimer",
            """
<h2>Risk Disclaimer</h2>
<p><strong>TRADING FUTURES AND CRYPTOCURRENCIES INVOLVES SUBSTANTIAL RISK OF LOSS.</strong></p>
<p>You should carefully consider whether trading is appropriate for you in light of your experience, objectives, financial resources, and other relevant circumstances.</p>
<ul style="color:#c9d8e8;line-height:2">
  <li>Futures markets use leverage, which magnifies both gains and losses.</li>
  <li>You can lose more than your initial deposit in futures trading.</li>
  <li>Cryptocurrency markets are highly volatile and unregulated in many jurisdictions.</li>
  <li>Past signal performance does not guarantee future results.</li>
  <li>AI-generated signals are probabilistic tools, not certainties.</li>
  <li>Never invest money you cannot afford to lose entirely.</li>
  <li>Diversify your investments and never risk your emergency funds.</li>
  <li>ARGUS QUANT is not a regulated financial advisor.</li>
</ul>
<p>By using this service you acknowledge that you have read, understood, and accepted this risk disclaimer.</p>
""",
        )
    )


# ── Sprint 22A — Portfolio exposure diagnostics ──────────────────────────────
@router.get("/api/portfolio/exposure")
async def api_portfolio_exposure():
    """Portfolio-level exposure picture built from currently OPEN signals.

    Read-only: reports the exposure score, open positions, correlation groups,
    long/short ratio and locked symbols. Always available (the engine flag only
    gates *enforcement* at signal time, not this diagnostic view)."""
    from app.risk.portfolio_exposure import build_state

    try:
        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(Signal)
                        .where(Signal.status == "OPEN")
                        .order_by(desc(Signal.created_at))
                    )
                )
                .scalars()
                .all()
            )
        positions = [
            {
                "symbol": s.symbol,
                "side": s.side,
                "status": "OPEN",
                "notional": float(getattr(s, "risk_reward", 0) or 0),
            }
            for s in rows
        ]
        state = build_state(open_positions=positions)
        return JSONResponse(
            {
                "enabled": settings.portfolio_exposure_engine_enabled,
                "limits": {
                    "max_open_positions_per_user": settings.max_open_positions_per_user,
                    "max_same_direction_positions": settings.max_same_direction_positions,
                    "max_correlated_positions": settings.max_correlated_positions,
                    "max_daily_loss_percent": settings.max_daily_loss_percent,
                },
                **state.to_diagnostics(),
            }
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── Sprint 22E — News / macro event risk ─────────────────────────────────────
@router.get("/api/public/news-risk")
async def api_public_news_risk():
    """Upcoming macro / event-risk windows and whether entries are currently
    blocked. The calendar is populated by the operator; empty == no events."""
    from app.risk.news_event_filter import news_risk_snapshot

    try:
        return JSONResponse(news_risk_snapshot())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
