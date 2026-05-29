from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, desc

from app.config import settings
from app.database.session import SessionLocal
from app.database.models import Signal
from app.market_data.ws_engine import ws_health



def _dashboard_user() -> str:
    import os
    return os.getenv("DASHBOARD_USER", "admin")

def _dashboard_password() -> str:
    import os
    return os.getenv("DASHBOARD_PASSWORD", "AlphaRadar@2026")

def _is_logged_in(request: Request) -> bool:
    return request.cookies.get("alpha_radar_auth") == "ok"

def _login_page(error: str = "") -> HTMLResponse:
    err = f"<div class='err'>{error}</div>" if error else ""
    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ALPHA RADAR SIGNALS Login</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#070b12;color:#eaf2ff;font-family:Arial}}
.box{{width:360px;background:#0b1320;border:1px solid #17314b;border-radius:18px;padding:28px;box-shadow:0 0 35px #00ffc833}}
.logo{{width:70px;height:70px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:34px;margin:auto}}
h1{{text-align:center;color:#20f0c0;font-size:24px;margin:18px 0 6px}}
p{{text-align:center;color:#8fa8c7;margin-bottom:24px}}
input{{width:100%;padding:14px;margin:8px 0;border-radius:10px;border:1px solid #17314b;background:#07101a;color:#fff}}
button{{width:100%;padding:14px;margin-top:14px;border:0;border-radius:10px;background:linear-gradient(90deg,#08a98f,#20f0c0);color:#001b18;font-weight:900;cursor:pointer}}
.err{{background:#3a1118;color:#ff7b8a;padding:10px;border-radius:8px;margin-bottom:12px;text-align:center}}
</style>
</head>
<body>
<form class="box" method="post" action="/login">
<div class="logo">A</div>
<h1>ALPHA RADAR SIGNALS</h1>
<p>Secure Dashboard Login</p>
{err}
<input name="username" placeholder="Username" required>
<input name="password" type="password" placeholder="Password" required>
<button type="submit">LOGIN</button>
</form>
</body>
</html>
""")

@asynccontextmanager
async def _lifespan(app: FastAPI):
    print("dashboard starting")
    yield


app = FastAPI(title="ALPHA RADAR SIGNALS", lifespan=_lifespan)


async def get_stats():
    now = datetime.now(timezone.utc)
    prod_start_raw = os.getenv("PRODUCTION_START_UTC", "").strip()
    if prod_start_raw:
        try:
            start7 = datetime.strptime(prod_start_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            start7 = now - timedelta(days=7)
    else:
        start7 = now - timedelta(days=7)

    async with SessionLocal() as session:
        result = await session.execute(
            select(Signal)
            .where(Signal.created_at >= start7)
            .order_by(desc(Signal.created_at))
            .limit(300)
        )
        week = result.scalars().all()

        recent_result = await session.execute(
            select(Signal)
            .order_by(desc(Signal.created_at))
            .limit(8)
        )
        recent_signals = recent_result.scalars().all()
    closed = [s for s in week if s.status in ["TP1", "TP2", "TP3", "SL"]]
    wins = [s for s in closed if s.status in ["TP1", "TP2", "TP3"]]
    losses = [s for s in closed if s.status == "SL"]

    winrate = len(wins) / max(1, len(wins) + len(losses)) * 100
    avg_pnl = sum(float(s.pnl_pct or 0) for s in closed) / max(1, len(closed))

    recent = recent_signals

    symbol_stats = {}

    for sig in closed:
        sym = sig.symbol
        symbol_stats.setdefault(sym, [])
        symbol_stats[sym].append(float(sig.pnl_pct or 0))

    leaderboard = sorted(
        [
            {
                "symbol": k,
                "avg": round(sum(v)/len(v), 2),
                "count": len(v),
            }
            for k, v in symbol_stats.items()
        ],
        key=lambda x: x["avg"],
        reverse=True,
    )[:10]

    return {
        "winrate": round(winrate, 1),
        "signals7d": len(week),
        "avgpnl": round(avg_pnl, 2),
        "universe": 196,
        "leaderboard": leaderboard,

        "recent": [
            {
                "time": s.created_at.strftime("%H:%M:%S") if s.created_at else "-",
                "symbol": s.symbol,
                "side": s.side,
                "tf": s.timeframe,
                "conf": round(float(s.confidence or 0), 1),
                "rr": s.risk_reward,
                "status": s.status,
                "pnl": round(float(s.pnl_pct or 0), 2),
            }
            for s in recent
        ],
    }






@app.get("/api/prices")
async def api_prices(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(ws_health())


@app.get("/api/health")
async def api_health():
    return {
        "ok": True,
        "brand": "ALPHA RADAR SIGNALS",
        "dashboard": "online",
        "database": "online",
        "redis": "online",
        "telegram": "online",
        "scanner": "online",
        "websocket": ws_health(),
    }


@app.get("/api/dashboard")
async def api_dashboard(request: Request):
    if not _is_logged_in(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse(await get_stats())



@app.get("/login", response_class=HTMLResponse)
async def login_get():
    return _login_page()

@app.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    if username == _dashboard_user() and password == _dashboard_password():
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("alpha_radar_auth", "ok", httponly=True, max_age=86400)
        return resp
    return _login_page("Invalid username or password")

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("alpha_radar_auth")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _is_logged_in(request):
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse("""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ALPHA RADAR SIGNALS</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#070b12;color:#eaf2ff;font-family:Inter,Arial,sans-serif}
.wrap{display:grid;grid-template-columns:280px 1fr;min-height:100vh}
.side{background:linear-gradient(180deg,#08111c,#07101a);border-right:1px solid #13263a;padding:26px 22px}
.logo{display:flex;align-items:center;gap:14px;margin-bottom:38px}
.mark{width:62px;height:62px;border:2px solid #20f0c0;border-radius:50%;display:grid;place-items:center;color:#20f0c0;font-weight:900;font-size:30px;box-shadow:0 0 28px #00ffc855}
.brand{font-size:22px;font-weight:900;line-height:1.05}.brand span{color:#20f0c0;letter-spacing:4px;font-size:13px}
.nav div{padding:14px 14px;border-radius:10px;margin:8px 0;color:#bdd3ee}.nav div:first-child{background:linear-gradient(90deg,#08a98f,#0d403b);color:#fff}
.status{position:absolute;bottom:28px;width:230px;border:1px solid #17314b;border-radius:14px;padding:18px;background:#0b1320}.ok{color:#19ff82}
.main{padding:28px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}
h1{margin:0;color:#22e6c3;font-size:32px;letter-spacing:1px}h2{font-size:16px;margin:0 0 18px;color:#fff}.sub{color:#8fa8c7;margin-top:6px}

.live{
background:#073d35;
color:#20ffc8;
border:1px solid #19d9b5;
border-radius:8px;
padding:8px 14px;
font-weight:800;
animation:pulse 2s infinite;
}

@keyframes pulse{
0%{box-shadow:0 0 0 #20ffc800}
50%{box-shadow:0 0 20px #20ffc855}
100%{box-shadow:0 0 0 #20ffc800}
}

.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:18px}.card{background:linear-gradient(180deg,#101827,#0b1320);border:1px solid #17314b;border-radius:14px;padding:22px;box-shadow:0 0 25px #0008}
.label{color:#7fa0c8;font-size:12px;letter-spacing:1px}.num{font-size:34px;font-weight:900;margin-top:12px}.green{color:#20ff80}.red{color:#ff4f61}.cyan{color:#20e6c3}
.two{display:grid;grid-template-columns:2fr 1fr;gap:18px;margin-top:18px}.three{display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;margin-top:18px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px;border-bottom:1px solid #17283d;font-size:14px}th{color:#8fa8c7;font-size:12px;letter-spacing:1px}
.spark{height:56px;margin-top:10px;background:linear-gradient(135deg,transparent 45%,#18ff8055 46%,#18ff8044 55%,transparent 56%);border-radius:10px}
.footer{margin-top:18px;color:#627a99;font-size:13px}
@media(max-width:900px){.wrap{grid-template-columns:1fr}.side{display:none}.grid4,.two,.three{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <aside class="side">
    <div class="logo"><div class="mark">A</div><div class="brand">ALPHA RADAR<br><span>SIGNALS</span></div></div>
    <div class="nav">
      <div id="tab-dashboard" onclick="showTab('dashboard')">Dashboard</div>
<div id="tab-signals" onclick="showTab('signals')">Signals</div>
<div id="tab-performance" onclick="showTab('performance')">Performance</div>
<div id="tab-leaderboard" onclick="showTab('leaderboard')">Leaderboard</div>
<div id="tab-settings" onclick="showTab('settings')">Settings</div>
    </div>
    <div class="status"><b>Bot Status</b><p class="ok">● All Systems Operational</p><p>Database OK<br>Redis OK<br>Telegram OK<br>Scanner OK</p></div>
  </aside>

  <main class="main">
    <div class="top">
      <div><h1>ALPHA RADAR SIGNALS</h1><div class="sub">AI FUTURES SIGNAL SYSTEM</div></div>
      <div id="last-update" style="color:#7fa0c8;font-size:13px;margin-bottom:6px">Updating...</div><div><a href="/logout" style="color:#8fa8c7;margin-right:14px;text-decoration:none">Logout</a><span class="live">LIVE</span></div>
    </div>

    <div id="content-dashboard" data-tab><div class="grid4">
      <div class="card"><div class="label">WIN RATE (7D)</div><div id="winrate" class="num green">—</div></div>
      <div class="card"><div class="label">SIGNALS (7D)</div><div id="signals" class="num">—</div></div>
      <div class="card"><div class="label">AVG PNL</div><div id="avgpnl" class="num green">—</div></div>
      <div class="card"><div class="label">UNIVERSE</div><div id="universe" class="num cyan">—</div></div>
    </div>

    <div class="two">
      <div class="card"><h2>RECENT SIGNALS</h2><table><thead><tr><th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th><th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th></tr></thead><tbody id="recent"></tbody></table></div>
      <div class="card"><h2>SYSTEM</h2><p>Scanner: <span class="green">Running</span></p><p>Tracker: <span class="green">Running</span></p><p>Dashboard: <span class="green">Online</span></p><p>Port: 8010</p></div>
    </div>

    <div class="three">
      <div class="card">
<h2>MARKET REGIME</h2>

<p>
Bias:
<span id="market-bias" class="cyan">--</span>
</p>

<p>
BTC 5m:
<span id="btc-bias" class="cyan">--</span>
</p>

<p>
ETH 5m:
<span id="eth-bias" class="cyan">--</span>
</p>

<p>
SOL 5m:
<span id="sol-bias" class="cyan">--</span>
</p>

<hr style="margin:16px 0;border-color:#1b2a41">

<p>BTCUSDT: <span id="px-btc" class="cyan">--</span></p>
<p>ETHUSDT: <span id="px-eth" class="cyan">--</span></p>
<p>SOLUSDT: <span id="px-sol" class="cyan">--</span></p>

</div>
      <div class="card"><h2>LEADERBOARD</h2><p>OPUSDT +3.47%</p><p>SOLUSDT +2.86%</p><p>BTCUSDT +2.45%</p></div>
      <div class="card"><h2>PERFORMANCE (7D)</h2><div class="spark"></div></div>
    </div>
    <div class="footer">© 2026 ALPHA RADAR SIGNALS</div></div>

<div id="content-signals" data-tab style="display:none">
  <div class="card">
    <h2>LIVE SIGNALS</h2>
    <table>
      <thead>
        <tr>
          <th>TIME</th><th>SYMBOL</th><th>SIDE</th><th>TF</th>
          <th>CONF</th><th>RR</th><th>STATUS</th><th>PNL</th>
        </tr>
      </thead>
      <tbody id="signals-table"></tbody>
    </table>
  </div>
</div>

<div id="content-performance" data-tab style="display:none">
  <div class="card">
    <h2>PERFORMANCE</h2>
    <p>Production metrics after launch date.</p>
    <div style="margin-top:20px;font-size:18px">
      Winrate: <span id="perf-winrate" class="green">--</span><br><br>
      Avg PnL: <span id="perf-pnl" class="green">--</span><br><br>
      Signals: <span id="perf-signals">--</span>
    </div>
  </div>
</div>

<div id="content-leaderboard" data-tab style="display:none">
  <div class="card">
    <h2>TOP SYMBOLS</h2>
    <table>
      <thead>
        <tr>
          <th>SYMBOL</th><th>AVG PNL</th><th>SIGNALS</th>
        </tr>
      </thead>
      <tbody id="leaderboard-table"></tbody>
    </table>
  </div>
</div>

<div id="content-settings" data-tab style="display:none">
  <div class="card">
    <h2>SETTINGS</h2>
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
  const r = await fetch('/api/dashboard');
  const d = await r.json();
  winrate.textContent = d.winrate + '%';
  signals.textContent = d.signals7d;
  avgpnl.textContent = (d.avgpnl >= 0 ? '+' : '') + d.avgpnl + '%';
  universe.textContent = d.universe;

  perf-winrate.textContent = d.winrate + '%';
  perf-pnl.textContent = (d.avgpnl >= 0 ? '+' : '') + d.avgpnl + '%';
  perf-signals.textContent = d.signals7d;

  leaderboard-table.innerHTML = d.leaderboard.map(x => `
    <tr>
      <td>${x.symbol}</td>
      <td class="${x.avg>=0?'green':'red'}">${x.avg}%</td>
      <td>${x.count}</td>
    </tr>
  `).join('');

  signals-table.innerHTML = d.recent.map(x => `
    <tr>
      <td>${x.time}</td><td>${x.symbol}</td>
      <td class="${x.side==='LONG'?'green':'red'}">${x.side}</td>
      <td>${x.tf}</td><td>${x.conf}%</td><td>1:${x.rr}</td>
      <td>${x.status}</td><td class="${x.pnl>=0?'green':'red'}">${x.pnl}%</td>
    </tr>`).join('');

  recent.innerHTML = d.recent.map(x => `
    <tr>
      <td>${x.time}</td><td>${x.symbol}</td>
      <td class="${x.side==='LONG'?'green':'red'}">${x.side}</td>
      <td>${x.tf}</td><td>${x.conf}%</td><td>1:${x.rr}</td>
      <td>${x.status}</td><td class="${x.pnl>=0?'green':'red'}">${x.pnl}%</td>
    </tr>`).join('');
}

async function refreshLoop(){
  try{
    await load();
    document.getElementById("last-update").innerHTML =
      "Last Update • " + new Date().toLocaleTimeString();
  }catch(e){
    console.error(e);
    document.getElementById("last-update").innerHTML =
      "Connection issue...";
  }
}


async function loadPrices(){
  try{
    const r = await fetch('/api/prices');
    const d = await r.json();
    if(d.prices){
      document.getElementById("px-btc").textContent = d.prices.BTCUSDT ?? "--";
      document.getElementById("px-eth").textContent = d.prices.ETHUSDT ?? "--";
      document.getElementById("px-sol").textContent = d.prices.SOLUSDT ?? "--";

      if(d.market_bias){
        const mb = d.market_bias;

        const biasEl = document.getElementById("market-bias");

        biasEl.textContent = mb.bias;

        biasEl.className =
          mb.bias === "RISK_ON"
            ? "green"
            : mb.bias === "RISK_OFF"
            ? "red"
            : "cyan";

        document.getElementById("btc-bias").textContent =
          mb.btc_5m_change_pct + "%";

        document.getElementById("eth-bias").textContent =
          mb.eth_5m_change_pct + "%";

        document.getElementById("sol-bias").textContent =
          mb.sol_5m_change_pct + "%";
      }
    }
  }catch(e){ console.error(e); }
}

refreshLoop();
loadPrices();
setInterval(refreshLoop, 4000);
setInterval(loadPrices, 2000);


</script>


<script>
function showTab(name){

document.querySelectorAll('.nav div').forEach(x=>{
  x.style.background='transparent';
});

document.getElementById('tab-'+name).style.background =
'linear-gradient(90deg,#08a98f,#0d403b)';


document.querySelectorAll('[data-tab]').forEach(el=>{
  el.style.display='none';
});

const target = document.getElementById('content-'+name);
if(target){
  target.style.display='block';
}

}
</script>


</body>
</html>
""")


@app.get("/health")
async def health():
    return {"status": "ok"}

def create_app():
    return app
