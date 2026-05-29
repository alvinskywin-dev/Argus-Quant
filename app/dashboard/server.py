from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse
from sqlalchemy import select, desc

from app.config import settings
from app.database.session import SessionLocal
from app.database.models import Signal
from app.market_data import universe
from app.market_data.ws_engine import ws_health


# ── auth ──────────────────────────────────────────────────────────

def _admin_user() -> str:
    return os.getenv("DASHBOARD_USER", "admin")


def _admin_password() -> str:
    return os.getenv("DASHBOARD_PASSWORD", "AlphaRadar@2026")


def _is_logged_in(request: Request) -> bool:
    return request.cookies.get("alpha_radar_auth") == "ok"


def _login_page(error: str = "") -> HTMLResponse:
    err = f"<div class='err'>{error}</div>" if error else ""
    return HTMLResponse(_LOGIN_HTML.replace("__ERR__", err))


# ── app ───────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    print("dashboard starting")
    yield


app = FastAPI(title="ALPHA RADAR SIGNALS", lifespan=_lifespan)
_boot_time = time.time()


# ── stats helper ──────────────────────────────────────────────────

async def _get_stats() -> dict:
    now = datetime.now(timezone.utc)
    prod_start_raw = os.getenv("PRODUCTION_START_UTC", "").strip()
    try:
        start7 = (
            datetime.strptime(prod_start_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if prod_start_raw else now - timedelta(days=7)
        )
    except Exception:
        start7 = now - timedelta(days=7)

    async with SessionLocal() as session:
        week_res = await session.execute(
            select(Signal).where(Signal.created_at >= start7)
            .order_by(desc(Signal.created_at)).limit(500)
        )
        week = week_res.scalars().all()
        recent_res = await session.execute(
            select(Signal).order_by(desc(Signal.created_at)).limit(20)
        )
        recent = recent_res.scalars().all()

    closed = [s for s in week if s.status in ("TP1", "TP2", "TP3", "SL")]
    open_sigs = [s for s in week if s.status == "OPEN"]
    wins = [s for s in closed if s.status in ("TP1", "TP2", "TP3")]
    losses = [s for s in closed if s.status == "SL"]
    winrate = len(wins) / max(1, len(wins) + len(losses)) * 100
    avg_pnl = sum(float(s.pnl_pct or 0) for s in closed) / max(1, len(closed))

    sym_map: dict = {}
    for s in closed:
        sym_map.setdefault(s.symbol, []).append(float(s.pnl_pct or 0))
    leaderboard = sorted(
        [{"symbol": k, "avg": round(sum(v) / len(v), 2), "count": len(v)} for k, v in sym_map.items()],
        key=lambda x: x["avg"], reverse=True,
    )[:10]

    def _row(s):
        return {
            "time": s.created_at.strftime("%m-%d %H:%M") if s.created_at else "-",
            "symbol": s.symbol, "side": s.side, "tf": s.timeframe,
            "conf": round(float(s.confidence or 0), 1),
            "rr": round(float(s.risk_reward or 0), 2),
            "status": s.status,
            "pnl": round(float(s.pnl_pct or 0), 2),
            "entry_low": round(float(s.entry_low or 0), 6),
            "tp1": round(float(s.tp1 or 0), 6),
            "sl": round(float(s.stop_loss or 0), 6),
        }

    return {
        "winrate": round(winrate, 1),
        "signals7d": len(week),
        "open_signals": len(open_sigs),
        "closed_signals": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "avgpnl": round(avg_pnl, 2),
        "universe": len(universe.symbols),
        "leaderboard": leaderboard,
        "recent": [_row(s) for s in recent],
        "open": [_row(s) for s in open_sigs[:15]],
        "closed_recent": [_row(s) for s in closed[:15]],
    }


# ── public API (no auth) ──────────────────────────────────────────

@app.get("/api/public/stats")
async def api_public_stats():
    try:
        return JSONResponse(await _get_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/public/prices")
async def api_public_prices():
    return JSONResponse(ws_health())


# ── monitoring (no auth) ──────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "uptime_sec": round(time.time() - _boot_time)}


@app.get("/api/health")
async def api_health():
    wsh = ws_health()
    return {
        "ok": True, "brand": "ALPHA RADAR SIGNALS", "dashboard": "online",
        "database": "online", "redis": "online", "telegram": "online",
        "scanner": "online", "websocket": wsh,
        "uptime_sec": round(time.time() - _boot_time),
    }


@app.get("/status")
async def status_route():
    wsh = ws_health()
    return {
        "status": "ok", "uptime_sec": round(time.time() - _boot_time),
        "universe": len(universe.symbols), "websocket": wsh,
        "config": {
            "min_confidence": settings.min_confidence,
            "scan_interval_sec": settings.scan_interval_sec,
            "max_signals_per_hour": settings.max_signals_per_hour,
            "timeframes": settings.timeframes,
        },
    }


@app.get("/metrics")
async def metrics():
    wsh = ws_health()
    lines = [
        "# HELP alpha_radar_universe_size Total symbols in universe",
        "# TYPE alpha_radar_universe_size gauge",
        f"alpha_radar_universe_size {len(universe.symbols)}",
        "# HELP alpha_radar_uptime_seconds Bot uptime in seconds",
        "# TYPE alpha_radar_uptime_seconds counter",
        f"alpha_radar_uptime_seconds {round(time.time() - _boot_time)}",
        "# HELP alpha_radar_ws_ok WebSocket price feed health (1=ok,0=stale)",
        "# TYPE alpha_radar_ws_ok gauge",
        f"alpha_radar_ws_ok {1 if wsh.get('ok') else 0}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ── admin API (requires auth) ─────────────────────────────────────

@app.get("/api/prices")
async def api_prices(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(ws_health())


@app.get("/api/dashboard")
async def api_dashboard(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        return JSONResponse(await _get_stats())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ── auth routes ───────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_get():
    return _login_page()


@app.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    if username == _admin_user() and password == _admin_password():
        resp = RedirectResponse("/admin", status_code=302)
        resp.set_cookie("alpha_radar_auth", "ok", httponly=True, max_age=86400)
        return resp
    return _login_page("Invalid username or password")


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("alpha_radar_auth")
    return resp


# ── admin dashboard ───────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request):
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(_ADMIN_HTML)


# ── public homepage ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    tg_url = os.getenv("TELEGRAM_CHANNEL_URL", "")
    dc_url = os.getenv("DISCORD_URL", "")
    trc20 = os.getenv("DONATE_USDT_TRC20", "")
    bep20 = os.getenv("DONATE_USDT_BEP20", "")
    btc_addr = os.getenv("DONATE_BTC", "")
    eth_addr = os.getenv("DONATE_ETH", "")
    binance_aff = os.getenv("BINANCE_AFFILIATE_URL", "")
    bybit_aff = os.getenv("BYBIT_AFFILIATE_URL", "")
    okx_aff = os.getenv("OKX_AFFILIATE_URL", "")
    bitget_aff = os.getenv("BITGET_AFFILIATE_URL", "")

    html = _PUBLIC_HTML

    # header nav community buttons
    tg_btn = (
        f'<a href="{tg_url}" target="_blank" style="background:#0088cc22;border:1px solid #0088cc;'
        f'color:#5bb7e3;padding:7px 13px;border-radius:7px;font-size:13px;text-decoration:none">Telegram</a>'
        if tg_url else ""
    )
    dc_btn = (
        f'<a href="{dc_url}" target="_blank" style="background:#5865f222;border:1px solid #5865f2;'
        f'color:#8b95f7;padding:7px 13px;border-radius:7px;font-size:13px;text-decoration:none">Discord</a>'
        if dc_url else ""
    )
    html = html.replace("__TG_BTN__", tg_btn).replace("__DC_BTN__", dc_btn)

    # hero CTA buttons
    hero_btns = []
    if tg_url:
        hero_btns.append(
            f'<a href="{tg_url}" target="_blank" style="background:linear-gradient(90deg,#0077b6,#00bbf0);'
            f'color:#fff;padding:14px 28px;border-radius:12px;font-weight:700;font-size:15px;text-decoration:none">✈ Join Telegram</a>'
        )
    if dc_url:
        hero_btns.append(
            f'<a href="{dc_url}" target="_blank" style="background:#5865f2;color:#fff;padding:14px 28px;'
            f'border-radius:12px;font-weight:700;font-size:15px;text-decoration:none">💬 Join Discord</a>'
        )
    if not hero_btns:
        hero_btns.append('<span style="color:#8fa8c7;font-size:15px">Free signals — join our community below</span>')
    html = html.replace("__HERO_BTNS__", "".join(hero_btns))

    # community section
    comm_cards = []
    if tg_url:
        comm_cards.append(
            f'<div class="comm-card"><div class="comm-ico">✈</div>'
            f'<div><div class="comm-t">Telegram Channel</div>'
            f'<div class="comm-d">Get free AI signals instantly in Telegram. Real-time alerts.</div>'
            f'<a href="{tg_url}" target="_blank" class="btn btntg">Join Free</a></div></div>'
        )
    if dc_url:
        comm_cards.append(
            f'<div class="comm-card"><div class="comm-ico">💬</div>'
            f'<div><div class="comm-t">Discord Server</div>'
            f'<div class="comm-d">Chat with traders, analysis channels and signal alerts.</div>'
            f'<a href="{dc_url}" target="_blank" class="btn btndc">Join Server</a></div></div>'
        )
    community_section = (
        '<div class="container"><section>'
        '<div class="stitle"><b></b>Join the Community</div>'
        '<div class="comm-grid">' + "".join(comm_cards) + '</div>'
        '</section></div>'
    ) if comm_cards else ""
    html = html.replace("__COMMUNITY__", community_section)

    # donate section
    don_cards = []
    if trc20:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">USDT</div><div class="don-net">TRC20 (Tron Network)</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{trc20}\')">{trc20}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    if bep20:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">USDT</div><div class="don-net">BEP20 (BSC Network)</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{bep20}\')">{bep20}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    if btc_addr:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">BTC</div><div class="don-net">Bitcoin Network</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{btc_addr}\')">{btc_addr}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    if eth_addr:
        don_cards.append(
            f'<div class="don-card"><div class="don-coin">ETH</div><div class="don-net">Ethereum (ERC20)</div>'
            f'<div class="don-addr" onclick="copyAddr(this,\'{eth_addr}\')">{eth_addr}</div>'
            f'<div class="don-hint">Click to copy address</div></div>'
        )
    donate_section = (
        '<div class="container"><section>'
        '<div class="stitle"><b></b>Support the Project</div>'
        '<p style="color:#8fa8c7;margin-bottom:18px;font-size:14px">All signals are 100% free. Donations help keep the servers running.</p>'
        '<div class="don-grid">' + "".join(don_cards) + '</div>'
        '</section></div>'
    ) if don_cards else ""
    html = html.replace("__DONATE__", donate_section)

    # affiliates section
    aff_cards = []
    exchanges = [
        ("Binance", binance_aff, "#f0b90b", "World's largest crypto exchange"),
        ("Bybit", bybit_aff, "#ffcc00", "Top derivatives & futures"),
        ("OKX", okx_aff, "#1a82ff", "Leading altcoin exchange"),
        ("Bitget", bitget_aff, "#00e6b3", "Copy trading platform"),
    ]
    for name, url, color, desc in exchanges:
        if url:
            aff_cards.append(
                f'<div class="aff-card"><div class="aff-name" style="color:{color}">{name}</div>'
                f'<div class="aff-desc">{desc}</div>'
                f'<a href="{url}" target="_blank" class="btn-aff">Register Free</a></div>'
            )
    aff_section = (
        '<div class="container"><section>'
        '<div class="stitle"><b></b>Recommended Exchanges</div>'
        '<p style="color:#8fa8c7;margin-bottom:18px;font-size:14px">Register through our partner links to support free signals.</p>'
        '<div class="aff-grid">' + "".join(aff_cards) + '</div>'
        '</section></div>'
    ) if aff_cards else ""
    html = html.replace("__AFFILIATES__", aff_section)

    return HTMLResponse(html)


def create_app():
    return app


# ═════════════════════════════════════════════════════════════════
#  HTML TEMPLATES
# ═════════════════════════════════════════════════════════════════

_LOGIN_HTML = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Admin Login — ALPHA RADAR SIGNALS</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#070b12;color:#eaf2ff;font-family:Arial}
.box{width:360px;background:#0b1320;border:1px solid #17314b;border-radius:18px;padding:28px;box-shadow:0 0 35px #00ffc822}
.logo{width:64px;height:64px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:32px;margin:auto}
h1{text-align:center;color:#20f0c0;font-size:22px;margin:16px 0 4px}
p{text-align:center;color:#8fa8c7;margin-bottom:22px;font-size:13px}
input{width:100%;padding:13px;margin:7px 0;border-radius:9px;border:1px solid #17314b;background:#07101a;color:#fff;box-sizing:border-box}
button{width:100%;padding:13px;margin-top:12px;border:0;border-radius:9px;background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;font-weight:900;cursor:pointer;font-size:15px}
.err{background:#3a1118;color:#ff7b8a;padding:9px;border-radius:7px;margin-bottom:10px;text-align:center;font-size:13px}
.back{text-align:center;margin-top:16px;font-size:12px}<a{color:#20e6c3}
</style>
</head>
<body>
<form class="box" method="post" action="/login">
<div class="logo">A</div>
<h1>ALPHA RADAR SIGNALS</h1>
<p>Admin Dashboard Login</p>
__ERR__
<input name="username" placeholder="Username" required autocomplete="username">
<input name="password" type="password" placeholder="Password" required autocomplete="current-password">
<button type="submit">LOGIN</button>
<div class="back"><a href="/" style="color:#8fa8c7;text-decoration:none">← Back to public site</a></div>
</form>
</body>
</html>
"""

_PUBLIC_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ALPHA RADAR SIGNALS — Free AI Crypto Futures Signals</title>
<meta name="description" content="Free AI-powered crypto futures signals. Multi-timeframe analysis. Real-time results."/>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#070b12;color:#eaf2ff;font-family:Inter,Arial,sans-serif;line-height:1.6}
a{color:#20e6c3;text-decoration:none}
.container{max-width:1200px;margin:0 auto;padding:0 24px}
header{background:#08111c;border-bottom:1px solid #13263a;position:sticky;top:0;z-index:100}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:13px 24px;max-width:1200px;margin:0 auto;gap:12px}
.logo{display:flex;align-items:center;gap:11px;flex-shrink:0}
.mark{width:42px;height:42px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:20px;box-shadow:0 0 16px #00ffc855;flex-shrink:0}
.brand{font-size:16px;font-weight:900;letter-spacing:1px;color:#eaf2ff}
.brand em{color:#20f0c0;font-style:normal}
.hnav{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.live{background:#073d35;color:#20ffc8;border:1px solid #19d9b5;border-radius:5px;padding:3px 9px;font-weight:800;font-size:11px;animation:pulse 2s infinite;white-space:nowrap}
@keyframes pulse{0%,100%{box-shadow:0 0 0 #20ffc800}50%{box-shadow:0 0 14px #20ffc855}}
.hero-wrap{background:radial-gradient(ellipse 80% 40% at 50% 0%,#0d2a1e,transparent);padding:68px 24px 48px;text-align:center}
.hero-wrap h1{font-size:46px;font-weight:900;letter-spacing:2px;margin-bottom:14px}
.hero-wrap h1 em{color:#20f0c0;font-style:normal}
.hero-wrap p{font-size:16px;color:#8fa8c7;max-width:540px;margin:0 auto 34px}
.hero-btns{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.sbar{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:28px 0}
.scard{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:19px;text-align:center}
.slabel{color:#7fa0c8;font-size:10px;letter-spacing:2px;text-transform:uppercase}
.sval{font-size:32px;font-weight:900;margin-top:8px}
.g{color:#20ff80}.r{color:#ff4f61}.c{color:#20e6c3}.y{color:#ffd84d}
section{padding:36px 0}
.stitle{font-size:19px;font-weight:900;margin-bottom:18px;display:flex;align-items:center;gap:10px}
.stitle b{width:4px;height:21px;background:linear-gradient(180deg,#08a98f,#20f0c0);border-radius:2px;display:inline-block;flex-shrink:0}
.card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:20px;box-shadow:0 0 18px #0005}
.mkt-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}
.mkt-card{text-align:center;padding:16px;background:#0b1320;border:1px solid #17314b;border-radius:10px}
.mkt-sym{font-size:10px;color:#8fa8c7;letter-spacing:1px;margin-bottom:5px;text-transform:uppercase}
.mkt-px{font-size:24px;font-weight:900}
.bias-row{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;background:#0b1320;border:1px solid #17314b;border-radius:10px;padding:13px}
.bi{text-align:center}.bi .bl{font-size:9px;color:#8fa8c7;margin-bottom:2px;text-transform:uppercase;letter-spacing:1px}.bi .bv{font-size:14px;font-weight:700}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #17283d;font-size:13px}
th{color:#8fa8c7;font-size:10px;letter-spacing:1px;text-transform:uppercase}
tr:last-child td{border-bottom:none}
.bl2{background:#0a3a1f44;color:#20ff80;border:1px solid #20ff8033;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}
.bs2{background:#3a0a1244;color:#ff4f61;border:1px solid #ff4f6133;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:700}
.bopen{color:#20ffc8;font-weight:700}.btp{color:#20ff80;font-weight:700}.bsl{color:#ff4f61;font-weight:700}.bexp{color:#ffd84d;font-weight:700}
.stats3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.tabs{display:flex;gap:7px;margin-bottom:14px}
.tbtn{padding:7px 15px;border-radius:7px;border:1px solid #17314b;background:transparent;color:#8fa8c7;cursor:pointer;font-size:12px}
.tbtn.act{background:#08a98f22;border-color:#20f0c0;color:#20f0c0}
.tpane{display:none}.tpane.act{display:block}
.lb-row{display:flex;align-items:center;gap:11px;padding:10px 0;border-bottom:1px solid #17283d}
.lb-row:last-child{border-bottom:none}
.lb-rank{font-size:16px;font-weight:900;width:28px;color:#8fa8c7}
.lb-sym{font-size:14px;font-weight:700;flex:1}
.lb-avg{font-size:14px;font-weight:900}.lb-cnt{font-size:10px;color:#8fa8c7}
.comm-grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.comm-card{background:#0b1320;border:1px solid #17314b;border-radius:11px;padding:20px;display:flex;align-items:flex-start;gap:14px}
.comm-ico{font-size:32px;line-height:1;flex-shrink:0}
.comm-t{font-size:14px;font-weight:700;margin-bottom:3px}
.comm-d{font-size:12px;color:#8fa8c7;margin-bottom:11px}
.btn{padding:8px 16px;border-radius:7px;font-weight:700;font-size:12px;display:inline-block;cursor:pointer;text-decoration:none}
.btntg{background:#0088cc;color:#fff}.btndc{background:#5865f2;color:#fff}
.don-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:13px}
.don-card{background:#0b1320;border:1px solid #17314b;border-radius:11px;padding:17px}
.don-coin{font-size:11px;font-weight:700;color:#8fa8c7;letter-spacing:1px;margin-bottom:3px}
.don-net{font-size:10px;color:#627a99;margin-bottom:7px}
.don-addr{background:#070b12;border:1px solid #17314b;border-radius:7px;padding:8px;font-family:monospace;font-size:11px;color:#20e6c3;word-break:break-all;cursor:pointer;transition:border-color .2s}
.don-addr:hover{border-color:#20f0c0}
.don-hint{font-size:10px;color:#627a99;margin-top:4px}
.aff-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:13px}
.aff-card{background:#0b1320;border:1px solid #17314b;border-radius:11px;padding:17px;text-align:center}
.aff-name{font-size:15px;font-weight:900;margin-bottom:4px}
.aff-desc{font-size:11px;color:#8fa8c7;margin-bottom:11px}
.btn-aff{background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;font-weight:700;font-size:12px;padding:7px 12px;border-radius:7px;display:block;text-align:center;text-decoration:none}
.disc{background:#1a0c0c;border:1px solid #5a1a1a;border-radius:13px;padding:20px;margin-bottom:40px}
.disc h3{color:#ff7b8a;margin-bottom:9px;font-size:14px;display:flex;align-items:center;gap:6px}
.disc p{color:#c57a7a;font-size:13px;line-height:1.7}
footer{border-top:1px solid #13263a;padding:26px 24px;text-align:center;color:#627a99;font-size:12px}
@media(max-width:860px){.sbar{grid-template-columns:1fr 1fr}.mkt-grid{grid-template-columns:1fr}.comm-grid{grid-template-columns:1fr}.aff-grid{grid-template-columns:1fr 1fr}.stats3{grid-template-columns:1fr}.hero-wrap h1{font-size:30px}}
@media(max-width:480px){.sbar{grid-template-columns:1fr}.aff-grid{grid-template-columns:1fr}.don-grid{grid-template-columns:1fr}}
</style>
</head>
<body>

<header>
<div class="hdr">
<div class="logo">
  <div class="mark">A</div>
  <div class="brand">ALPHA RADAR <em>SIGNALS</em></div>
</div>
<div class="hnav">
  <span class="live">● LIVE</span>
  __TG_BTN__
  __DC_BTN__
  <a href="/admin" style="color:#627a99;font-size:11px;padding:5px 11px;border:1px solid #17314b;border-radius:6px">Admin</a>
</div>
</div>
</header>

<div class="hero-wrap">
<div class="container">
  <h1>ALPHA RADAR <em>SIGNALS</em></h1>
  <p>Free AI-powered crypto futures signals. Multi-timeframe analysis. Real-time results. No subscription.</p>
  <div class="hero-btns">__HERO_BTNS__</div>
</div>
</div>

<div class="container">
<div class="sbar">
  <div class="scard"><div class="slabel">WIN RATE (7D)</div><div id="s-wr" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">SIGNALS (7D)</div><div id="s-tot" class="sval">—</div></div>
  <div class="scard"><div class="slabel">AVG PNL</div><div id="s-pnl" class="sval g">—</div></div>
  <div class="scard"><div class="slabel">UNIVERSE</div><div id="s-uni" class="sval c">—</div></div>
</div>

<section>
<div class="stitle"><b></b>Live Market Overview</div>
<div class="card">
  <div class="mkt-grid">
    <div class="mkt-card"><div class="mkt-sym">BTC/USDT</div><div id="px-btc" class="mkt-px c">—</div></div>
    <div class="mkt-card"><div class="mkt-sym">ETH/USDT</div><div id="px-eth" class="mkt-px c">—</div></div>
    <div class="mkt-card"><div class="mkt-sym">SOL/USDT</div><div id="px-sol" class="mkt-px c">—</div></div>
  </div>
  <div class="bias-row">
    <div class="bi"><div class="bl">MARKET BIAS</div><div id="b-ov" class="bv c">—</div></div>
    <div class="bi"><div class="bl">BTC 5M</div><div id="b-btc" class="bv">—</div></div>
    <div class="bi"><div class="bl">ETH 5M</div><div id="b-eth" class="bv">—</div></div>
    <div class="bi"><div class="bl">SOL 5M</div><div id="b-sol" class="bv">—</div></div>
    <div class="bi"><div class="bl">UPDATED</div><div id="b-upd" class="bv" style="color:#8fa8c7;font-size:11px">—</div></div>
  </div>
</div>
</section>

<section>
<div class="stitle"><b></b>Latest Signals</div>
<div class="card" style="overflow-x:auto">
<table>
<thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
<tbody id="sig-tbl"><tr><td colspan="8" style="text-align:center;color:#627a99;padding:24px">Loading signals...</td></tr></tbody>
</table>
</div>
</section>

<section>
<div class="stitle"><b></b>Performance Statistics (7D)</div>
<div class="stats3">
  <div class="card" style="text-align:center">
    <div class="slabel">WIN RATE</div>
    <div id="ps-wr" class="sval g" style="font-size:38px;margin:10px 0">—</div>
    <div style="font-size:12px;color:#627a99"><span id="ps-w" class="g">—</span> wins &nbsp;/&nbsp; <span id="ps-l" class="r">—</span> losses</div>
  </div>
  <div class="card" style="text-align:center">
    <div class="slabel">AVG PNL / TRADE</div>
    <div id="ps-pnl" class="sval g" style="font-size:38px;margin:10px 0">—</div>
    <div style="font-size:12px;color:#627a99">Closed trades only</div>
  </div>
  <div class="card" style="text-align:center">
    <div class="slabel">OPEN NOW</div>
    <div id="ps-open" class="sval c" style="font-size:38px;margin:10px 0">—</div>
    <div style="font-size:12px;color:#627a99">Active signals</div>
  </div>
</div>
</section>

<section>
<div class="stitle"><b></b>Signal History</div>
<div class="card">
<div class="tabs">
  <button class="tbtn act" onclick="swTab('open',this)">Open (<span id="tc-open">—</span>)</button>
  <button class="tbtn" onclick="swTab('closed',this)">Closed</button>
</div>
<div id="tp-open" class="tpane act" style="overflow-x:auto">
<table>
<thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>ENTRY</th><th>TP1</th><th>SL</th><th>CONF</th><th>RR</th></tr></thead>
<tbody id="open-tbl"><tr><td colspan="9" style="text-align:center;color:#627a99;padding:18px">No open signals</td></tr></tbody>
</table>
</div>
<div id="tp-closed" class="tpane" style="overflow-x:auto">
<table>
<thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>RESULT</th><th>PNL</th></tr></thead>
<tbody id="closed-tbl"><tr><td colspan="8" style="text-align:center;color:#627a99;padding:18px">Loading...</td></tr></tbody>
</table>
</div>
</div>
</section>

<section>
<div class="stitle"><b></b>Performance Leaderboard</div>
<div class="card"><div id="lb-list"><p style="color:#8fa8c7;text-align:center;padding:24px">Loading...</p></div></div>
</section>
</div>

__COMMUNITY__
__DONATE__
__AFFILIATES__

<div class="container">
<div class="disc">
<h3>⚠ Risk Disclaimer</h3>
<p>Signals are for educational purposes only. Trading futures is high risk. Past performance is not indicative of future results. You may lose all your capital. Never trade with money you cannot afford to lose. Alpha Radar Signals does not provide financial, investment, or legal advice. Always do your own research. By using this service you acknowledge and accept all trading risks.</p>
</div>
</div>

<footer>
<p style="font-size:15px;font-weight:700;color:#eaf2ff;margin-bottom:5px">ALPHA RADAR SIGNALS</p>
<p>Free AI-powered crypto futures signals &nbsp;·&nbsp; For educational use only</p>
<p style="margin-top:8px"><a href="/admin" style="color:#627a99;font-size:11px">Admin Dashboard</a></p>
<p style="margin-top:8px;font-size:11px">© 2026 ALPHA RADAR SIGNALS &nbsp;·&nbsp; Not financial advice.</p>
</footer>

<script>
function pct(v){return(v===null||v===undefined)?'—':(v>=0?'+':'')+v+'%';}
function cls(v){return v>=0?'g':'r';}
function sBadge(st){
  if(st==='OPEN')return '<span class="bopen">OPEN</span>';
  if(st==='SL')return '<span class="bsl">SL</span>';
  if(st==='EXPIRED')return '<span class="bexp">EXP</span>';
  return '<span class="btp">'+st+'</span>';
}
function swTab(n,btn){
  document.querySelectorAll('.tbtn').forEach(b=>b.classList.remove('act'));
  btn.classList.add('act');
  document.querySelectorAll('.tpane').forEach(p=>p.classList.remove('act'));
  document.getElementById('tp-'+n).classList.add('act');
}
async function copyAddr(el,addr){
  try{
    await navigator.clipboard.writeText(addr);
    el.style.borderColor='#20f0c0';
    el.title='Copied!';
    setTimeout(()=>{el.style.borderColor='';el.title='';},1600);
  }catch(e){}
}
async function loadStats(){
  try{
    const r=await fetch('/api/public/stats');
    if(!r.ok)return;
    const d=await r.json();
    if(d.error)return;
    document.getElementById('s-wr').textContent=d.winrate+'%';
    document.getElementById('s-tot').textContent=d.signals7d;
    const pe=document.getElementById('s-pnl');
    pe.textContent=pct(d.avgpnl);pe.className='sval '+cls(d.avgpnl);
    document.getElementById('s-uni').textContent=d.universe;
    document.getElementById('ps-wr').textContent=d.winrate+'%';
    const ppe=document.getElementById('ps-pnl');
    ppe.textContent=pct(d.avgpnl);ppe.className='sval '+cls(d.avgpnl);
    document.getElementById('ps-w').textContent=d.wins??'—';
    document.getElementById('ps-l').textContent=d.losses??'—';
    document.getElementById('ps-open').textContent=d.open_signals??'—';
    document.getElementById('tc-open').textContent=d.open_signals??'—';
    // latest signals
    const sRows=(d.recent||[]).slice(0,15).map(x=>
      '<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.tf+'</td><td>'+x.conf+'%</td><td>1:'+x.rr+'</td>'+
      '<td>'+sBadge(x.status)+'</td>'+
      '<td class="'+cls(x.pnl)+'">'+pct(x.pnl)+'</td></tr>'
    ).join('');
    document.getElementById('sig-tbl').innerHTML=sRows||
      '<tr><td colspan="8" style="text-align:center;color:#627a99;padding:20px">No signals yet</td></tr>';
    // open history
    const oRows=(d.open||[]).map(x=>
      '<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.tf+'</td><td>'+x.entry_low+'</td><td>'+x.tp1+'</td><td>'+x.sl+'</td>'+
      '<td>'+x.conf+'%</td><td>1:'+x.rr+'</td></tr>'
    ).join('');
    document.getElementById('open-tbl').innerHTML=oRows||
      '<tr><td colspan="9" style="text-align:center;color:#627a99;padding:18px">No open signals</td></tr>';
    // closed history
    const cRows=(d.closed_recent||[]).map(x=>
      '<tr><td>'+x.time+'</td><td><b>'+x.symbol+'</b></td>'+
      '<td><span class="'+(x.side==='LONG'?'bl2':'bs2')+'">'+x.side+'</span></td>'+
      '<td>'+x.tf+'</td><td>'+x.conf+'%</td><td>1:'+x.rr+'</td>'+
      '<td>'+sBadge(x.status)+'</td>'+
      '<td class="'+cls(x.pnl)+'">'+pct(x.pnl)+'</td></tr>'
    ).join('');
    document.getElementById('closed-tbl').innerHTML=cRows||
      '<tr><td colspan="8" style="text-align:center;color:#627a99;padding:18px">No closed signals</td></tr>';
    // leaderboard
    const lbHtml=(d.leaderboard||[]).map((x,i)=>{
      const clr=i===0?'#ffd84d':i===1?'#c0c0c0':i===2?'#cd7f32':'#8fa8c7';
      return '<div class="lb-row">'+
        '<div class="lb-rank" style="color:'+clr+'">#'+(i+1)+'</div>'+
        '<div class="lb-sym">'+x.symbol+'</div>'+
        '<div style="text-align:right">'+
          '<div class="lb-avg '+cls(x.avg)+'">'+pct(x.avg)+'</div>'+
          '<div class="lb-cnt">'+x.count+' signals</div>'+
        '</div></div>';
    }).join('');
    document.getElementById('lb-list').innerHTML=lbHtml||
      '<p style="color:#8fa8c7;text-align:center;padding:20px">No data yet</p>';
  }catch(e){console.error(e);}
}
async function loadPrices(){
  try{
    const r=await fetch('/api/public/prices');
    if(!r.ok)return;
    const d=await r.json();
    if(d.prices){
      document.getElementById('px-btc').textContent=d.prices.BTCUSDT??'—';
      document.getElementById('px-eth').textContent=d.prices.ETHUSDT??'—';
      document.getElementById('px-sol').textContent=d.prices.SOLUSDT??'—';
    }
    if(d.market_bias){
      const mb=d.market_bias;
      const bEl=document.getElementById('b-ov');
      bEl.textContent=mb.bias??'—';
      bEl.className='bv '+(mb.bias==='RISK_ON'?'g':mb.bias==='RISK_OFF'?'r':'c');
      document.getElementById('b-btc').textContent=(mb.btc_5m_change_pct??'—')+'%';
      document.getElementById('b-eth').textContent=(mb.eth_5m_change_pct??'—')+'%';
      document.getElementById('b-sol').textContent=(mb.sol_5m_change_pct??'—')+'%';
    }
    document.getElementById('b-upd').textContent=new Date().toLocaleTimeString();
  }catch(e){console.error(e);}
}
loadStats();loadPrices();
setInterval(loadStats,6000);
setInterval(loadPrices,3000);
</script>
</body>
</html>
"""

_ADMIN_HTML = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Admin — ALPHA RADAR SIGNALS</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#070b12;color:#eaf2ff;font-family:Inter,Arial,sans-serif}
.wrap{display:grid;grid-template-columns:270px 1fr;min-height:100vh}
.side{background:linear-gradient(180deg,#08111c,#07101a);border-right:1px solid #13263a;padding:22px 18px}
.logo{display:flex;align-items:center;gap:12px;margin-bottom:32px}
.mark{width:56px;height:56px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:26px;box-shadow:0 0 22px #00ffc855}
.brand{font-size:19px;font-weight:900;line-height:1.1}.brand span{color:#20f0c0;letter-spacing:3px;font-size:11px;display:block}
.nav div{padding:12px;border-radius:9px;margin:6px 0;color:#bdd3ee;cursor:pointer;font-size:14px}
.nav div:hover{background:#0f2030}
.nav div.act{background:linear-gradient(90deg,#08a98f,#0d403b);color:#fff}
.status{position:absolute;bottom:24px;width:224px;border:1px solid #17314b;border-radius:12px;padding:16px;background:#0b1320;font-size:13px}.ok{color:#19ff82}
.main{padding:24px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}
h1{margin:0;color:#22e6c3;font-size:28px;letter-spacing:1px}h2{font-size:15px;margin:0 0 16px;color:#fff}.sub{color:#8fa8c7;margin-top:4px;font-size:13px}
.live{background:#073d35;color:#20ffc8;border:1px solid #19d9b5;border-radius:7px;padding:6px 12px;font-weight:800;font-size:12px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 #20ffc800}50%{box-shadow:0 0 18px #20ffc855}}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:15px}
.card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:13px;padding:20px;box-shadow:0 0 20px #0006}
.label{color:#7fa0c8;font-size:11px;letter-spacing:1px}.num{font-size:30px;font-weight:900;margin-top:10px}
.g{color:#20ff80}.r{color:#ff4f61}.c{color:#20e6c3}
.two{display:grid;grid-template-columns:2fr 1fr;gap:15px;margin-top:15px}
.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:15px;margin-top:15px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #17283d;font-size:13px}
th{color:#8fa8c7;font-size:11px;letter-spacing:1px}tr:last-child td{border-bottom:none}
.spark{height:50px;margin-top:8px;background:linear-gradient(135deg,transparent 45%,#18ff8044 46%,#18ff8033 55%,transparent 56%);border-radius:8px}
.footer{margin-top:15px;color:#627a99;font-size:12px}
[data-tab]{display:none}[data-tab].show{display:block}
@media(max-width:860px){.wrap{grid-template-columns:1fr}.side{display:none}.grid4,.two,.three{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
<aside class="side">
  <div class="logo"><div class="mark">A</div><div class="brand">ALPHA RADAR<span>SIGNALS</span></div></div>
  <div class="nav">
    <div id="nav-dashboard" class="act" onclick="showTab('dashboard')">Dashboard</div>
    <div id="nav-signals" onclick="showTab('signals')">Signals</div>
    <div id="nav-performance" onclick="showTab('performance')">Performance</div>
    <div id="nav-leaderboard" onclick="showTab('leaderboard')">Leaderboard</div>
    <div id="nav-settings" onclick="showTab('settings')">Settings</div>
  </div>
  <div class="status"><b>Bot Status</b><p class="ok">● All Systems Operational</p><p style="color:#8fa8c7;margin-top:6px">Database OK<br>Redis OK<br>Telegram OK<br>Scanner OK</p></div>
</aside>
<main class="main">
  <div class="top">
    <div><h1>ALPHA RADAR SIGNALS</h1><div class="sub">Admin Dashboard</div></div>
    <div style="display:flex;align-items:center;gap:12px">
      <div id="last-update" style="color:#7fa0c8;font-size:12px">Updating...</div>
      <a href="/" style="color:#8fa8c7;font-size:13px;text-decoration:none">Public Site</a>
      <a href="/logout" style="color:#8fa8c7;font-size:13px;text-decoration:none">Logout</a>
      <span class="live">LIVE</span>
    </div>
  </div>

  <div id="tab-dashboard" data-tab class="show">
    <div class="grid4">
      <div class="card"><div class="label">WIN RATE (7D)</div><div id="winrate" class="num g">—</div></div>
      <div class="card"><div class="label">SIGNALS (7D)</div><div id="signals" class="num">—</div></div>
      <div class="card"><div class="label">AVG PNL</div><div id="avgpnl" class="num g">—</div></div>
      <div class="card"><div class="label">UNIVERSE</div><div id="universe" class="num c">—</div></div>
    </div>
    <div class="two">
      <div class="card"><h2>RECENT SIGNALS</h2>
        <table><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
        <tbody id="recent"></tbody></table>
      </div>
      <div class="card"><h2>SYSTEM</h2>
        <p>Scanner: <span class="g">Running</span></p>
        <p>Tracker: <span class="g">Running</span></p>
        <p>Dashboard: <span class="g">Online</span></p>
        <p style="margin-top:8px;color:#8fa8c7;font-size:12px">Port: 8010</p>
      </div>
    </div>
    <div class="three">
      <div class="card"><h2>MARKET REGIME</h2>
        <p>Bias: <span id="market-bias" class="c">--</span></p>
        <p>BTC 5m: <span id="btc-bias" class="c">--</span></p>
        <p>ETH 5m: <span id="eth-bias" class="c">--</span></p>
        <p>SOL 5m: <span id="sol-bias" class="c">--</span></p>
        <hr style="margin:14px 0;border-color:#1b2a41">
        <p>BTCUSDT: <span id="px-btc" class="c">--</span></p>
        <p>ETHUSDT: <span id="px-eth" class="c">--</span></p>
        <p>SOLUSDT: <span id="px-sol" class="c">--</span></p>
      </div>
      <div class="card"><h2>LEADERBOARD</h2><div id="dash-lb">Loading...</div></div>
      <div class="card"><h2>PERFORMANCE (7D)</h2><div class="spark"></div><div id="perf-sum" style="margin-top:10px;color:#8fa8c7;font-size:13px"></div></div>
    </div>
    <div class="footer">© 2026 ALPHA RADAR SIGNALS</div>
  </div>

  <div id="tab-signals" data-tab>
    <div class="card"><h2>LIVE SIGNALS</h2>
      <table><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead>
      <tbody id="signals-table"></tbody></table>
    </div>
  </div>

  <div id="tab-performance" data-tab>
    <div class="card"><h2>PERFORMANCE</h2>
      <div style="margin-top:18px;font-size:17px">
        Winrate: <span id="perf-winrate" class="g">--</span><br><br>
        Avg PnL: <span id="perf-pnl" class="g">--</span><br><br>
        Signals: <span id="perf-signals">--</span>
      </div>
    </div>
  </div>

  <div id="tab-leaderboard" data-tab>
    <div class="card"><h2>TOP SYMBOLS</h2>
      <table><thead><tr><th>SYMBOL</th><th>AVG PNL</th><th>SIGNALS</th></tr></thead>
      <tbody id="lb-table"></tbody></table>
    </div>
  </div>

  <div id="tab-settings" data-tab>
    <div class="card"><h2>SETTINGS</h2>
      <p>VIP/Public routing enabled</p>
      <p>AI scoring active</p>
      <p>Daily reports active</p>
      <p>Production filters enabled</p>
    </div>
  </div>
</main>
</div>

<script>
async function load(){
  const r=await fetch('/api/dashboard');
  if(!r.ok)return;
  const d=await r.json();
  if(d.error)return;
  document.getElementById('winrate').textContent=d.winrate+'%';
  document.getElementById('signals').textContent=d.signals7d;
  document.getElementById('avgpnl').textContent=(d.avgpnl>=0?'+':'')+d.avgpnl+'%';
  document.getElementById('universe').textContent=d.universe;
  document.getElementById('perf-winrate').textContent=d.winrate+'%';
  document.getElementById('perf-pnl').textContent=(d.avgpnl>=0?'+':'')+d.avgpnl+'%';
  document.getElementById('perf-signals').textContent=d.signals7d;
  document.getElementById('perf-sum').innerHTML='Win rate: '+d.winrate+'%<br>Avg PnL: '+(d.avgpnl>=0?'+':'')+d.avgpnl+'%';
  const lbH=(d.leaderboard||[]).map(x=>
    '<p><code>'+x.symbol+'</code> <span class="'+(x.avg>=0?'g':'r')+'">'+(x.avg>=0?'+':'')+x.avg+'%</span> ('+x.count+')</p>'
  ).join('');
  document.getElementById('dash-lb').innerHTML=lbH||'<p style="color:#8fa8c7">No data yet</p>';
  document.getElementById('lb-table').innerHTML=(d.leaderboard||[]).map(x=>
    '<tr><td>'+x.symbol+'</td><td class="'+(x.avg>=0?'g':'r')+'">'+(x.avg>=0?'+':'')+x.avg+'%</td><td>'+x.count+'</td></tr>'
  ).join('');
  const rows=(d.recent||[]).map(x=>
    '<tr><td>'+x.time+'</td><td>'+x.symbol+'</td>'+
    '<td class="'+(x.side==='LONG'?'g':'r')+'">'+x.side+'</td>'+
    '<td>'+x.tf+'</td><td>'+x.conf+'%</td><td>1:'+x.rr+'</td>'+
    '<td>'+x.status+'</td><td class="'+(x.pnl>=0?'g':'r')+'">'+x.pnl+'%</td></tr>'
  ).join('');
  document.getElementById('recent').innerHTML=rows;
  document.getElementById('signals-table').innerHTML=rows;
}
async function loadPx(){
  try{
    const r=await fetch('/api/prices');
    if(!r.ok)return;
    const d=await r.json();
    if(d.prices){
      document.getElementById('px-btc').textContent=d.prices.BTCUSDT??'--';
      document.getElementById('px-eth').textContent=d.prices.ETHUSDT??'--';
      document.getElementById('px-sol').textContent=d.prices.SOLUSDT??'--';
      if(d.market_bias){
        const mb=d.market_bias;
        const bEl=document.getElementById('market-bias');
        bEl.textContent=mb.bias;
        bEl.className=mb.bias==='RISK_ON'?'g':mb.bias==='RISK_OFF'?'r':'c';
        document.getElementById('btc-bias').textContent=mb.btc_5m_change_pct+'%';
        document.getElementById('eth-bias').textContent=mb.eth_5m_change_pct+'%';
        document.getElementById('sol-bias').textContent=mb.sol_5m_change_pct+'%';
      }
    }
  }catch(e){}
}
async function tick(){
  try{
    await load();
    document.getElementById('last-update').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById('last-update').textContent='Connection issue...';
  }
}
tick();loadPx();
setInterval(tick,5000);setInterval(loadPx,2000);
function showTab(name){
  document.querySelectorAll('[data-tab]').forEach(el=>el.classList.remove('show'));
  document.querySelectorAll('.nav div').forEach(el=>el.classList.remove('act'));
  const t=document.getElementById('tab-'+name);
  if(t)t.classList.add('show');
  const n=document.getElementById('nav-'+name);
  if(n)n.classList.add('act');
}
</script>
</body>
</html>
"""
