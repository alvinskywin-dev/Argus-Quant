/* ALPHA RADAR SIGNALS — V12 SaaS portal (vanilla JS, hash-router SPA). */
(() => {
"use strict";

// ── token store ───────────────────────────────────────────────────
const TK = {
  get a(){return localStorage.getItem("ar_access")},
  get r(){return localStorage.getItem("ar_refresh")},
  set(a,r){a&&localStorage.setItem("ar_access",a);r&&localStorage.setItem("ar_refresh",r)},
  clear(){localStorage.removeItem("ar_access");localStorage.removeItem("ar_refresh")},
};
let ME = null;            // current user (UserOut) or null
let REFRESHING = null;    // in-flight refresh promise

// ── api client ─────────────────────────────────────────────────────
async function raw(path, {method="GET", body=null, auth=true}={}) {
  const h = {};
  if (body) h["Content-Type"] = "application/json";
  if (auth && TK.a) h["Authorization"] = "Bearer " + TK.a;
  const res = await fetch(path, {method, headers:h, body: body?JSON.stringify(body):null});
  let data = null;
  try { data = await res.json(); } catch(_) {}
  return {res, data};
}
async function api(path, opts={}) {
  let {res, data} = await raw(path, opts);
  if (res.status === 401 && opts.auth !== false && TK.r) {
    if (!REFRESHING) REFRESHING = raw("/api/auth/refresh", {method:"POST", body:{refresh_token:TK.r}, auth:false})
      .then(({res:r,data:d}) => { REFRESHING=null; if (r.ok && d?.access_token){TK.set(d.access_token,d.refresh_token);return true;} TK.clear(); return false; });
    const ok = await REFRESHING;
    if (ok) ({res,data} = await raw(path, opts));
  }
  if (!res.ok) {
    const err = new Error((data&&(data.detail||data.error))||("HTTP "+res.status));
    err.status = res.status; err.detail = err.message; throw err;
  }
  return data;
}
const pub = (p) => api(p, {auth:false});

// ── perf: short-TTL cache for public/idempotent GETs (dedupes route churn)
const CACHE = new Map();                 // path -> {t, p}
const CACHE_TTL = 20000;                 // 20s — Phase 12
function cachedGet(path, ttl=CACHE_TTL){
  const now=Date.now(), e=CACHE.get(path);
  if(e && now-e.t < ttl) return e.p;
  const p = tryGet(path).then(r=>{ if(r && r.error) CACHE.delete(path); return r; });
  CACHE.set(path,{t:now,p});
  return p;
}
function invalidate(){ CACHE.clear(); }   // after a mutation
function refresh(){ invalidate(); route(); }

// ── perf: Chart.js lifecycle — destroy before recreate, skip if unchanged
const CHARTS = {};
function mkChart(key, ctx, cfg, sig){
  const ex = CHARTS[key];
  if(ex){ if(sig!=null && ex.sig===sig) return ex.chart; try{ex.chart.destroy();}catch(_){} }
  const chart = new Chart(ctx, cfg);
  CHARTS[key] = {chart, sig};
  return chart;
}
function destroyCharts(){ for(const k in CHARTS){try{CHARTS[k].chart.destroy();}catch(_){}} for(const k in CHARTS) delete CHARTS[k]; }

// ── perf: per-page timers — only the active page auto-refreshes
let PAGE_TIMERS = [];
function every(ms, fn){ const id=setInterval(fn, ms); PAGE_TIMERS.push(id); return id; }
function clearPageTimers(){ PAGE_TIMERS.forEach(clearInterval); PAGE_TIMERS = []; }

// ── dom + format helpers ───────────────────────────────────────────
const $ = (s,r=document)=>r.querySelector(s);
const h = (html)=>{const t=document.createElement("template");t.innerHTML=html.trim();return t.content.firstElementChild;};
const esc = (s)=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = (n,d=2)=>{const v=Number(n);return isNaN(v)?"—":v.toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits: d>2?0:0});};
const money = (n,d=2)=>{const v=Number(n||0);return (v<0?"-$":"$")+Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:d,minimumFractionDigits:2});};
const pct = (n)=>{const v=Number(n||0);return (v>=0?"+":"")+v.toFixed(2)+"%";};
const cls = (n)=>Number(n||0)>=0?"pos":"neg";
const when = (s)=>{if(!s)return "—";try{return new Date(s).toLocaleString();}catch(_){return s;}};
const ago = (s)=>{if(!s)return "—";const d=(Date.now()-new Date(s))/1000;if(d<60)return Math.floor(d)+"s";if(d<3600)return Math.floor(d/60)+"m";if(d<86400)return Math.floor(d/3600)+"h";return Math.floor(d/86400)+"d";};
const badge=(v,extra="")=>`<span class="badge ${esc(v)} ${extra}">${esc(v)}</span>`;
const dot=(b)=>`<span class="dot ${b?"on":"off"}"></span>`;
const maskIp=(ip)=>{ if(!ip)return "—"; if(ip.includes(":")){const p=ip.split(":");return p.slice(0,2).join(":")+":••••";} const p=ip.split("."); return p.length===4?`${p[0]}.${p[1]}.•••.•••`:ip; };

function toast(msg, kind=""){const w=$("#toasts")||document.body.appendChild(h('<div id="toasts"></div>'));const t=h(`<div class="toast ${kind}">${esc(msg)}</div>`);w.appendChild(t);setTimeout(()=>{t.style.opacity="0";t.style.transition=".3s";setTimeout(()=>t.remove(),300);},3200);}
function modal(title, bodyHtml, opts={}){
  closeModal();
  const o=h(`<div class="overlay show" id="modal"><div class="modal ${opts.wide?"wide":""}"><div class="mh"><h3>${esc(title)}</h3><button class="x">&times;</button></div><div class="mb">${bodyHtml}</div></div></div>`);
  o.addEventListener("click",e=>{if(e.target.id==="modal")closeModal();});
  o.querySelector(".x").onclick=closeModal;
  document.body.appendChild(o); return o;
}
const closeModal=()=>{const m=$("#modal");if(m)m.remove();};
// confirmation modal (Phase 8/11) — replaces native confirm()
function confirmModal({title, body="", confirmText="Confirm", danger=false, onConfirm}){
  const o=modal(title, `${body}<div class="row" style="justify-content:flex-end;margin-top:18px">
    <button class="btn" id="cm-no">Cancel</button>
    <button class="btn ${danger?"danger":"primary"}" id="cm-yes">${esc(confirmText)}</button></div>`, {wide:false});
  o.querySelector("#cm-no").onclick=closeModal;
  o.querySelector("#cm-yes").onclick=(e)=>withLoading(e.currentTarget, async()=>{ try{ await onConfirm(); closeModal(); }catch(err){ toast(err.detail||"Failed","bad"); } });
}
// button loading state (Phase 11)
async function withLoading(btn, fn){
  if(!btn) return fn();
  btn.classList.add("loading"); btn.disabled=true;
  try{ return await fn(); }
  finally{ btn.classList.remove("loading"); btn.disabled=false; }
}

const skel=(rows=5)=>`<div class="card-b">${Array(rows).fill('<div class="sk row"></div>').join("")}</div>`;
const empty=(ic,title,sub="")=>`<div class="empty"><div class="ic">${ic}</div><h4>${esc(title)}</h4><div>${esc(sub)}</div></div>`;
const disabledCard=(feature,flag)=>`<div class="card pad">${empty("🔒",feature+" is disabled","Set "+flag+"=true and restart to enable this module.")}</div>`;
function stat(lab,val,delta="",ic=""){return `<div class="stat"><div class="lab">${ic} ${esc(lab)}</div><div class="val">${val}</div>${delta?`<div class="delta">${delta}</div>`:""}</div>`;}
function tableWrap(head, rowsHtml){return `<div class="t-wrap"><table><thead><tr>${head.map(x=>`<th>${x}</th>`).join("")}</tr></thead><tbody>${rowsHtml}</tbody></table></div>`;}

// guarded fetch: returns {data} or {disabled:true} on 404, {error} otherwise
async function tryGet(path){
  try { return {data: await api(path)}; }
  catch(e){ if(e.status===404) return {disabled:true}; if(e.status===401){logout();return {error:"auth"};} return {error:e.detail||"error"}; }
}

// ── navigation model ───────────────────────────────────────────────
const NAV = [
  {grp:"Trading"},
  {id:"dashboard", t:"Dashboard", ic:"📊"},
  {id:"analytics", t:"Signal Analytics", ic:"📈"},
  {id:"paper", t:"Paper Trading", ic:"🧪"},
  {id:"live", t:"Live Trading", ic:"⚡"},
  {grp:"Account"},
  {id:"exchange", t:"Exchange Vault", ic:"🔑"},
  {id:"auto", t:"Auto Trading", ic:"🤖"},
  {id:"safety", t:"Safety Center", ic:"🛡️"},
  {id:"profile", t:"Profile", ic:"👤"},
  {grp:"Admin", admin:true},
  {id:"admin", t:"Platform", ic:"🛠️", admin:true},
];
const TITLES = {dashboard:["Dashboard","Platform & market overview"],analytics:["Signal Analytics","Signal performance & distributions"],
  paper:["Paper Trading","Risk-free demo futures account"],live:["Live Trading","MOCK by default — gate-protected"],
  exchange:["Exchange Vault","Encrypted API key management"],auto:["Auto Trading","DEMO auto-execution engine"],
  safety:["Safety Center","Loss limits & kill switches"],profile:["Profile","Account & security"],admin:["Admin Platform","Operator oversight"]};

// ── shell ──────────────────────────────────────────────────────────
function renderShell(){
  const isAdmin = ME && ME.role==="ADMIN";
  const nav = NAV.filter(n=>!n.admin||isAdmin).map(n=> n.grp
    ? `<div class="grp">${n.grp}</div>`
    : `<a data-route="${n.id}"><span class="ic">${n.ic}</span>${n.t}</a>`).join("");
  document.body.innerHTML = `
  <div class="scrim" id="scrim"></div>
  <div class="shell">
    <aside class="side" id="side">
      <div class="brand"><div class="mark">A</div><div><b>ALPHA RADAR</b><span>SIGNALS</span></div></div>
      <nav class="nav">${nav}
        <div class="grp">Links</div>
        <a href="/" ><span class="ic">🌐</span>Public Site</a>
        <a id="logout"><span class="ic">⎋</span>Logout</a>
      </nav>
    </aside>
    <div class="main">
      <div class="topbar">
        <div style="display:flex;align-items:center;gap:12px">
          <button class="burger" id="burger">☰</button>
          <div><h2 id="ptitle">Dashboard</h2><div class="sub" id="psub"></div></div>
        </div>
        <div class="right">
          <span class="badge muted" id="gatebadge">gate…</span>
          <div class="userchip"><div class="avatar">${esc((ME.email||"?")[0].toUpperCase())}</div>
            <div class="uname"><div style="font-weight:700">${esc(ME.username||ME.email.split("@")[0])}</div></div>
            ${badge(ME.role)}</div>
        </div>
      </div>
      <div class="content" id="view"></div>
    </div>
  </div>
  <div id="toasts"></div>`;
  $("#logout").onclick=logout;
  $("#burger").onclick=()=>{$("#side").classList.toggle("open");$("#scrim").classList.toggle("show");};
  $("#scrim").onclick=()=>{$("#side").classList.remove("open");$("#scrim").classList.remove("show");};
  document.querySelectorAll("[data-route]").forEach(a=>a.onclick=()=>{location.hash="#/"+a.dataset.route;});
  // live gate badge (top-right) — cached so it doesn't refetch on every route
  cachedGet("/api/live/status").then(r=>{const b=$("#gatebadge");if(!b)return;if(r.data){b.outerHTML=`<span class="badge ${r.data.live_gate_open?"LIVE":"MOCK"}">${r.data.mode} MODE</span>`;}else b.outerHTML=`<span class="badge MOCK">MOCK MODE</span>`;});
}

function setActive(route){
  document.querySelectorAll("[data-route]").forEach(a=>a.classList.toggle("active",a.dataset.route===route));
  const t=TITLES[route]||["",""];$("#ptitle").textContent=t[0];$("#psub").textContent=t[1];
  $("#side").classList.remove("open");$("#scrim").classList.remove("show");
}

// ── auth views ─────────────────────────────────────────────────────
function renderLanding(){
  document.body.innerHTML = `
  <div class="auth-wrap">
    <div class="auth-hero">
      <div class="tag">PROFESSIONAL CRYPTO FUTURES INTELLIGENCE</div>
      <h1>Trade smarter with<br><span style="color:var(--primary)">Alpha Radar Signals</span></h1>
      <p>Multi-timeframe SMC signal engine, market-regime detection, short-protection, paper & demo auto-trading, an encrypted multi-exchange vault, and a hard-gated live layer — one platform.</p>
      <div class="feat">
        <div><b>Market Regime</b>Bull / bear / volatility classification on every signal.</div>
        <div><b>Paper Trading</b>Risk-free 10,000 USDT demo futures accounts.</div>
        <div><b>Multi-Exchange</b>Binance · OKX · Bybit · Bitget adapters.</div>
        <div><b>Safety Layer</b>Loss limits, kill switches & global stop.</div>
      </div>
    </div>
    <div class="auth-card"><div class="auth-box">
      <h3>Sign in</h3><div class="mut">Access your trading portal.</div>
      <div id="autherr"></div>
      <div class="field"><label>Email</label><input id="email" type="email" placeholder="you@example.com" autocomplete="username"></div>
      <div class="field"><label>Password</label><input id="pw" type="password" placeholder="••••••••" autocomplete="current-password"></div>
      <div class="field hide" id="totpf"><label>2FA Code</label><input id="totp" inputmode="numeric" placeholder="123456"></div>
      <button class="btn primary" id="loginbtn" style="width:100%">Sign In</button>
      <div class="mut" style="margin-top:14px;text-align:center">No account? <a id="showreg">Create one</a></div>
    </div></div>
  </div><div id="toasts"></div>`;
  const doLogin=async()=>{
    const email=$("#email").value.trim(), password=$("#pw").value, totp_code=$("#totp")?.value||undefined;
    if(!email||!password)return;
    await withLoading($("#loginbtn"), async()=>{
      try{
        const d=await api("/api/auth/login",{method:"POST",auth:false,body:{email,password,totp_code}});
        if(d.two_factor_required){$("#totpf").classList.remove("hide");$("#autherr").innerHTML=`<div class="alert info">Enter your 2FA code.</div>`;return;}
        TK.set(d.access_token,d.refresh_token); await boot();
      }catch(e){
        if(e.status===404)$("#autherr").innerHTML=`<div class="alert warn">Auth API is disabled. Start the server with <b>AUTH_ENABLED=true</b>.</div>`;
        else $("#autherr").innerHTML=`<div class="alert danger">${esc(e.detail||"Login failed")}</div>`;
      }
    });
  };
  $("#loginbtn").onclick=doLogin;
  $("#pw").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin();});
  $("#showreg").onclick=renderRegister;
}
function renderRegister(){
  $("#autherr") && ($("#autherr").innerHTML="");
  const box=$(".auth-box"); if(!box)return;
  box.innerHTML=`<h3>Create account</h3><div class="mut">Start with a paper-trading account.</div>
    <div id="autherr"></div>
    <div class="field"><label>Email</label><input id="email" type="email" placeholder="you@example.com"></div>
    <div class="field"><label>Password</label><input id="pw" type="password" placeholder="min 8 characters"></div>
    <button class="btn primary" id="regbtn" style="width:100%">Create Account</button>
    <div class="mut" style="margin-top:14px;text-align:center">Have an account? <a id="showlogin">Sign in</a></div>`;
  $("#regbtn").onclick=()=>withLoading($("#regbtn"), async()=>{
    const email=$("#email").value.trim(),password=$("#pw").value;
    if(!email||password.length<8){$("#autherr").innerHTML=`<div class="alert warn">Email and 8+ char password required.</div>`;return;}
    try{
      await api("/api/auth/register",{method:"POST",auth:false,body:{email,password}});
      const d=await api("/api/auth/login",{method:"POST",auth:false,body:{email,password}});
      TK.set(d.access_token,d.refresh_token); await boot();
    }catch(e){$("#autherr").innerHTML=`<div class="alert danger">${esc(e.status===404?"Auth disabled (AUTH_ENABLED=true).":e.detail)}</div>`;}
  });
  $("#showlogin").onclick=renderLanding;
}
async function logout(){ try{ if(TK.r) await api("/api/auth/logout",{method:"POST",body:{refresh_token:TK.r}}); }catch(_){}
  clearPageTimers(); destroyCharts(); invalidate();
  TK.clear(); ME=null; location.hash=""; renderLanding(); }

// ── PAGES ──────────────────────────────────────────────────────────
const PAGES = {};

// signal feed renderer (shared) — Symbol/Side/TF/Confidence/RR/Status/PnL
function signalRows(rows){
  return rows.map(s=>{
    const open = (s.status||"").toUpperCase()==="OPEN";
    const pnlCell = open ? '<span class="sub">live</span>'
      : `<span class="num ${cls(s.pnl_pct)}">${pct(s.pnl_pct)}</span>`;
    return `<tr><td><b>${esc(s.symbol)}</b></td><td>${badge(s.side)}</td><td><span class="badge muted">${esc(s.timeframe)}</span></td>
      <td class="num">${num(s.confidence,1)}</td><td class="num">${num(s.risk_reward,2)}</td>
      <td>${badge(s.status,open?"":"muted")}</td><td>${pnlCell}</td></tr>`;
  }).join("");
}

PAGES.dashboard = async (v) => {
  v.innerHTML = `<div class="kpis" id="kpis">${Array(4).fill('<div class="sk kpi"></div>').join("")}</div>
    <div class="grid g2 mt">
      <div class="card"><div class="card-h"><h3>Market Regime</h3><span class="sub" id="rg-when"></span></div><div class="card-b" id="regime">${skel(2)}</div></div>
      <div class="card"><div class="card-h"><h3>System Health</h3><span class="dot off" id="hdot"></span></div><div class="card-b" id="health">${skel(3)}</div></div>
    </div>
    <div class="card mt"><div class="card-h"><h3>Live Signals</h3><a href="#/analytics" class="btn sm">Analytics →</a></div><div id="signals">${skel(5)}</div></div>
    <div class="card mt"><div class="card-h"><h3>Winrate Summary</h3></div><div class="card-b" id="wrsum">${skel(2)}</div></div>`;

  // KPIs — user-centric for everyone, platform KPIs added for admins
  const [status, wr, regime, acct] = await Promise.all([
    cachedGet("/status"), cachedGet("/api/public/winrate-analysis"),
    cachedGet("/api/public/market-regime"), tryGet("/api/paper/account/"),
  ]);
  const S = status.data, W = wr.data, A = acct.data;
  const winrate = W ? ((W.long_winrate+W.short_winrate)/2).toFixed(1)+"%" : "—";
  const kpis = [
    stat("Avg Winrate", winrate, W?("long "+W.long_winrate+"% · short "+W.short_winrate+"%"):"","🎯"),
    stat("Signal Sample", W?W.sample_size:"—","best TF "+(W?W.best_timeframe:"—"),"📡"),
    stat("Your Equity", A?money(A.equity):"—", A?(A.open_positions+" open · uPnL "+money(A.unrealized_pnl)):"","💰"),
    stat("System Health", S?(S.status==="ok"?'<span class="pos">HEALTHY</span>':'<span class="neg">DEGRADED</span>'):"—", S?("universe "+S.universe):"","💚"),
  ];
  if(ME.role==="ADMIN"){
    const ov = await cachedGet("/api/admin/overview"); const O=ov.data;
    if(O) kpis.push(
      stat("Active Users", O.users.total, "admins "+(O.users.by_role.ADMIN||0),"👥"),
      stat("Connected Exch", O.exchange_accounts.connected, Object.keys(O.exchange_accounts.by_exchange||{}).join(", ")||"none","🔗"),
      stat("Auto Users", O.auto_trading_enabled_users, "kills "+O.user_kill_switches_active,"🤖"),
      stat("Global Kill", O.global_kill?'<span class="neg">ON</span>':'<span class="pos">OFF</span>', O.live_gate_open?"gate OPEN":"gate closed","🛑"),
    );
  }
  $("#kpis").innerHTML = kpis.join("");

  // regime
  const rEl=$("#regime");
  if(regime.data){const r=regime.data;const score=Math.max(0,Math.min(100,r.regime_score||0));
    const color=r.market_regime.includes("BULL")||r.market_regime.includes("LOW")?"var(--success)":r.market_regime.includes("BEAR")||r.market_regime.includes("HIGH")?"var(--danger)":"var(--warning)";
    $("#rg-when").textContent=ago(r.calculated_at)+" ago";
    rEl.innerHTML=`<div class="regime"><div class="big r-${esc(r.market_regime)}">${esc(r.market_regime.replace("_"," "))}</div>
      <div class="gauge"><i style="width:${score}%;background:${color}"></i></div><b style="color:${color}">${score}</b></div>
      <div class="row" style="margin-top:14px">
        <span class="badge muted">BTC ${esc(r.btc_trend)}</span><span class="badge muted">ETH ${esc(r.eth_trend)}</span>
        <span class="badge muted">Breadth ${num(r.breadth,1)}%</span><span class="badge muted">ATR pct ${num(r.atr_percentile,1)}</span></div>`;
  } else rEl.innerHTML=empty("🌍","Regime unavailable");

  // health
  const hEl=$("#health");
  if(S){const ws=S.websocket||{}; const okAll=S.status==="ok";
    const hd=$("#hdot"); if(hd){hd.className="dot "+(okAll?"on":"warn");}
    hEl.innerHTML=[["Scanner",S.status==="ok"],["WebSocket",ws.ok],["Database",true],["Redis",true]].map(([k,ok])=>
      `<div class="kv"><span>${dot(ok)} ${k}</span><span class="badge ${ok?"ok":"bad"}">${ok?"OK":"DOWN"}</span></div>`).join("")+
      `<div class="kv"><span>Universe</span><span>${S.universe} symbols</span></div>
       <div class="kv"><span>Last price update</span><span>${num(ws.last_update_age_sec,1)}s ago</span></div>
       <div class="kv"><span>Uptime</span><span>${Math.floor((S.uptime_sec||0)/60)}m</span></div>`;
  } else hEl.innerHTML=empty("💔","Status unavailable");

  // live signals feed
  const renderSignals = async()=>{
    const sg=await cachedGet("/api/public/signals?limit=12");const rows=sg.data||[];
    const el=$("#signals"); if(!el) return;
    el.innerHTML = rows.length
      ? tableWrap(["Symbol","Side","TF","Conf","RR","Status","PnL"], signalRows(rows))
      : empty("📭","No recent signals","Signals appear here as the scanner detects setups.");
  };
  await renderSignals();

  // winrate mini summary
  const ws=$("#wrsum");
  if(W){ ws.innerHTML=`<div class="grid g3">
      <div class="kv"><span>Long winrate</span><b class="${cls(W.long_winrate-50)}">${num(W.long_winrate,1)}%</b></div>
      <div class="kv"><span>Short winrate</span><b class="${cls(W.short_winrate-50)}">${num(W.short_winrate,1)}%</b></div>
      <div class="kv"><span>Average</span><b>${winrate}</b></div>
      <div class="kv"><span>Best confidence</span><b>${esc(W.best_confidence_bucket)}</b></div>
      <div class="kv"><span>Best RR</span><b>${esc(W.best_rr_bucket)}</b></div>
      <div class="kv"><span>Best timeframe</span><b>${esc(W.best_timeframe)}</b></div></div>`;
  } else ws.innerHTML=empty("📉","No winrate data");

  // refresh ONLY this page's live data on an interval (Phase 11/12)
  every(20000, renderSignals);
};

PAGES.analytics = async (v) => {
  const wr=await cachedGet("/api/public/winrate-analysis");
  if(!wr.data){v.innerHTML=`<div class="card pad">${empty("📉","Analytics unavailable")}</div>`;return;}
  const W=wr.data;
  v.innerHTML=`<div class="kpis" id="asum">
      ${stat("Sample size",W.sample_size,"closed signals","📊")}
      ${stat("Long winrate",`<span class="${cls(W.long_winrate-50)}">${num(W.long_winrate,1)}%</span>`,"","🟢")}
      ${stat("Short winrate",`<span class="${cls(W.short_winrate-50)}">${num(W.short_winrate,1)}%</span>`,"","🔴")}
      ${stat("Best confidence",W.best_confidence_bucket||"—","","✨")}
      ${stat("Best RR",W.best_rr_bucket||"—","best TF "+(W.best_timeframe||"—"),"⚖️")}
    </div>
    <div class="grid g2 mt">
      <div class="card"><div class="card-h"><h3>Confidence vs Winrate</h3></div><div class="card-b"><div class="chart-box"><canvas id="c1"></canvas></div></div></div>
      <div class="card"><div class="card-h"><h3>Long vs Short Winrate</h3></div><div class="card-b"><div class="chart-box"><canvas id="c2"></canvas></div></div></div>
      <div class="card"><div class="card-h"><h3>RR Buckets</h3></div><div class="card-b"><div class="chart-box"><canvas id="c3"></canvas></div></div></div>
      <div class="card"><div class="card-h"><h3>Signals per Confidence</h3></div><div class="card-b"><div class="chart-box"><canvas id="c4"></canvas></div></div></div>
    </div>
    <div class="card mt"><div class="card-h"><h3>Confidence Buckets</h3></div><div id="buckets"></div></div>`;

  const cb=W.confidence_buckets||[], rb=W.rr_buckets||[];
  // buckets table (moved here from dashboard)
  $("#buckets").innerHTML = cb.length
    ? tableWrap(["Confidence","Trades","Winrate"], cb.map(b=>`<tr><td>${esc(b.label)}</td><td class="num">${b.total??b.count??"—"}</td><td class="num ${cls((b.winrate||0)-50)}">${num(b.winrate,1)}%</td></tr>`).join(""))
    : empty("—","No bucket data");

  if(!window.Chart) return;
  const gridc="#17314b", txtc="#a3b8d4";
  const opt=(extra={})=>Object.assign({responsive:true,maintainAspectRatio:false,plugins:{legend:{labels:{color:txtc}}},scales:{x:{ticks:{color:txtc},grid:{color:gridc}},y:{ticks:{color:txtc},grid:{color:gridc}}}},extra);
  const sig=JSON.stringify({cb,rb,l:W.long_winrate,s:W.short_winrate});
  mkChart("c1",$("#c1"),{type:"bar",data:{labels:cb.map(b=>b.label),datasets:[{label:"Winrate %",data:cb.map(b=>b.winrate||0),backgroundColor:"#20f0c0aa",borderRadius:6}]},options:opt()},sig);
  mkChart("c2",$("#c2"),{type:"doughnut",data:{labels:["Long","Short"],datasets:[{data:[W.long_winrate,W.short_winrate],backgroundColor:["#22c55e","#ef4444"]}]},options:{responsive:true,maintainAspectRatio:false,cutout:"62%",plugins:{legend:{labels:{color:txtc}}}}},sig);
  mkChart("c3",$("#c3"),{type:"bar",data:{labels:rb.map(b=>b.label),datasets:[{label:"Winrate %",data:rb.map(b=>b.winrate||0),backgroundColor:"#f59e0baa",borderRadius:6}]},options:opt()},sig);
  mkChart("c4",$("#c4"),{type:"bar",data:{labels:cb.map(b=>b.label),datasets:[{label:"Trades",data:cb.map(b=>b.total??b.count??0),backgroundColor:"#38bdf8aa",borderRadius:6}]},options:opt()},sig);
};

PAGES.paper = async (v) => {
  const a=await tryGet("/api/paper/account/");
  if(a.disabled) return void(v.innerHTML=disabledCard("Paper Trading","PAPER_TRADING_ENABLED"));
  if(a.error) return void(v.innerHTML=`<div class="card pad">${empty("⚠️","Could not load",a.error)}</div>`);
  const A=a.data;
  v.innerHTML=`<div class="kpis">
      ${stat("Balance",money(A.balance),"initial "+money(A.initial_balance),"💰")}
      ${stat("Equity",money(A.equity),"uPnL "+money(A.unrealized_pnl),"📊")}
      ${stat("Total PnL",`<span class="${cls(A.total_pnl)}">${money(A.total_pnl)}</span>`,"realized "+money(A.realized_pnl),"📈")}
      ${stat("Win Rate",num(A.win_rate,1)+"%",A.total_trades+" trades","🎯")}
      ${stat("Open Positions",A.open_positions,"used margin "+money(A.used_margin),"📂")}
      ${stat("Available",money(A.available_balance),"lev "+A.default_leverage+"x","🟢")}
      ${stat("Daily PnL",`<span class="${cls(A.daily_pnl)}">${money(A.daily_pnl)}</span>`,"","📅")}
      ${stat("Auto-Follow",A.auto_follow?'<span class="pos">ON</span>':'<span class="neg">OFF</span>',"signals","🔁")}
    </div>
    <div class="row" style="margin:16px 0;align-items:center">
      <label class="switch"><input type="checkbox" id="af" ${A.auto_follow?"checked":""}><span class="sl"></span></label>
      <span style="align-self:center">Auto-follow signals</span>
      <button class="btn sm" id="reset" style="margin-left:auto">Reset Account</button>
    </div>
    <div class="tabs"><button class="active" data-t="open">Open Positions</button><button data-t="closed">Trade History</button></div>
    <div id="ptab">${skel(4)}</div>`;
  $("#af").onchange=async e=>{try{await api("/api/paper/account/auto-follow",{method:"POST",body:{enabled:e.target.checked}});toast("Auto-follow "+(e.target.checked?"enabled":"disabled"),"ok");}catch(err){toast(err.detail,"bad");}};
  $("#reset").onclick=()=>confirmModal({title:"Reset paper account",body:`<div class="alert warn">This resets your demo account to its initial balance of ${money(A.initial_balance)} and closes all paper positions.</div>`,confirmText:"Reset Account",danger:true,onConfirm:async()=>{await api("/api/paper/account/reset",{method:"POST"});toast("Account reset","ok");refresh();}});
  const tabs=v.querySelectorAll(".tabs button");
  const loadTab=async(t)=>{
    tabs.forEach(b=>b.classList.toggle("active",b.dataset.t===t));
    const box=$("#ptab"); box.innerHTML=`<div class="card">${skel(4)}</div>`;
    if(t==="open"){
      const p=await tryGet("/api/paper/account/positions?status=open");const rows=p.data||[];
      if(!rows.length){ box.innerHTML=`<div class="card">${empty("📭","No open positions","Copy a signal or open a position to start.")}</div>`; return; }
      // premium position cards (responsive — also works for mobile/tablet)
      box.innerHTML=`<div class="pcards">${rows.map(r=>`<div class="pcard">
        <div class="ph"><b>${esc(r.symbol)}</b><span>${badge(r.side)} <span class="badge muted">${r.leverage}x</span></span></div>
        <div class="pg">
          <div><div class="k">Entry</div><div class="num">${num(r.entry_price,4)}</div></div>
          <div><div class="k">Mark</div><div class="num">${r.mark_price?num(r.mark_price,4):"—"}</div></div>
          <div><div class="k">Qty</div><div class="num">${num(r.quantity,4)}</div></div>
          <div><div class="k">ROE</div><div class="num ${cls(r.roe_pct)}">${r.roe_pct!=null?pct(r.roe_pct):"—"}</div></div>
        </div>
        <div class="pnl ${cls(r.unrealized_pnl)}">${r.unrealized_pnl!=null?money(r.unrealized_pnl):"—"}</div>
        <button class="btn sm" data-pos="${r.id}" style="margin-top:10px;width:100%">View Detail</button>
      </div>`).join("")}</div>`;
      box.querySelectorAll("[data-pos]").forEach(b=>b.onclick=()=>posDetail(rows.find(x=>x.id==b.dataset.pos)));
    } else {
      const tr=await tryGet("/api/paper/account/trades");const rows=tr.data||[];
      box.innerHTML=rows.length?`<div class="card">${tableWrap(["Symbol","Side","Entry","Exit","PnL","When"],
        rows.map(r=>`<tr><td><b>${esc(r.symbol)}</b></td><td>${badge(r.side)}</td><td class="num">${num(r.entry_price,4)}</td>
          <td class="num">${num(r.exit_price,4)}</td><td class="num ${cls(r.pnl_usdt)}">${money(r.pnl_usdt)}</td><td class="num">${ago(r.closed_at)} ago</td></tr>`).join(""))}</div>`
        :`<div class="card">${empty("📜","No trade history yet","Closed paper trades will be listed here.")}</div>`;
    }
  };
  tabs.forEach(b=>b.onclick=()=>loadTab(b.dataset.t));
  loadTab("open");
};
function posDetail(p){if(!p)return;modal(p.symbol+" position",
  `<div class="kv"><span>Side</span>${badge(p.side)}</div><div class="kv"><span>Entry</span><b>${num(p.entry_price,6)}</b></div>
   <div class="kv"><span>Mark</span><b>${p.mark_price?num(p.mark_price,6):"—"}</b></div><div class="kv"><span>Quantity</span><b>${num(p.quantity,6)}</b></div>
   <div class="kv"><span>Notional</span><b>${money(p.notional_usdt)}</b></div><div class="kv"><span>Leverage</span><b>${p.leverage}x</b></div>
   <div class="kv"><span>Liquidation</span><b>${num(p.liquidation_price,6)}</b></div>
   <div class="kv"><span>SL / TP1</span><b>${num(p.stop_loss,4)} / ${num(p.tp1,4)}</b></div>
   <div class="kv"><span>Unrealized PnL</span><b class="${cls(p.unrealized_pnl)}">${p.unrealized_pnl!=null?money(p.unrealized_pnl):"—"} (${p.roe_pct!=null?pct(p.roe_pct):"—"})</b></div>`);}

PAGES.exchange = async (v) => {
  const a=await tryGet("/api/exchange/accounts");
  if(a.disabled) return void(v.innerHTML=disabledCard("Exchange Vault","EXCHANGE_API_VAULT_ENABLED"));
  if(a.error) return void(v.innerHTML=`<div class="card pad">${empty("⚠️","Could not load",a.error)}</div>`);
  const byEx={}; (a.data||[]).forEach(x=>byEx[x.exchange]=x);
  const EX=[["binance","Binance"],["okx","OKX"],["bybit","Bybit"],["bitget","Bitget"]];
  // status → badge with icon (Phase 6)
  const statusBadge=(acc)=>{
    if(!acc) return '<span class="badge muted">○ NOT CONNECTED</span>';
    if(acc.status==="CONNECTED") return '<span class="badge CONNECTED">✓ CONNECTED</span>';
    if(!acc.last_test) return '<span class="badge TEST">⟳ TEST REQUIRED</span>';
    return badge(acc.status);
  };
  v.innerHTML=`<div class="alert info">🔒 API secrets are encrypted (AES-256-GCM) and never displayed. Withdrawal-enabled keys are rejected. Live execution stays <b>MOCK Safe</b> until the global live gate is opened.</div>
    <div class="grid g2">${EX.map(([id,name])=>{
      const acc=byEx[id]; const conn=acc&&acc.status==="CONNECTED";
      return `<div class="card pad"><div class="row" style="justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:12px"><img src="/static/exchanges/${id}.svg" onerror="this.style.display='none'" style="width:34px;height:34px"><div><b style="font-size:16px">${name}</b><div class="sub" style="margin-top:4px">${statusBadge(acc)} <span class="badge MOCK">🛡 MOCK Safe</span></div></div></div>
        <div>${conn?`<button class="btn sm" data-test="${id}">Test</button> <button class="btn sm danger" data-disc="${id}">Disconnect</button>`:`<button class="btn sm primary" data-conn="${id}" data-name="${name}">Connect</button>`}</div></div>
        ${acc?`<div class="row" style="margin-top:12px;gap:18px"><span class="sub">Key ••••${esc(acc.api_key_last4||"????")}</span>
          <span class="sub">Trade ${dot(acc.can_trade)}</span><span class="sub">Futures ${dot(acc.can_futures)}</span><span class="sub">Withdraw ${dot(acc.can_withdraw)}</span>
          <span class="sub" style="margin-left:auto">Tested ${acc.last_test?ago(acc.last_test)+" ago":"—"}</span></div>${acc.last_error?`<div class="alert danger" style="margin:10px 0 0">${esc(acc.last_error)}</div>`:""}`:""}
      </div>`;}).join("")}</div>`;
  v.querySelectorAll("[data-conn]").forEach(b=>b.onclick=()=>connectModal(b.dataset.conn,b.dataset.name));
  v.querySelectorAll("[data-test]").forEach(b=>b.onclick=()=>withLoading(b, async()=>{try{const r=await api("/api/exchange/test",{method:"POST",body:{exchange:b.dataset.test}});toast("Test: "+(r.detail||r.status||"ok"),"ok");refresh();}catch(e){toast(e.detail,"bad");}}));
  v.querySelectorAll("[data-disc]").forEach(b=>b.onclick=()=>confirmModal({title:"Disconnect "+b.dataset.disc,body:`<div class="alert warn">Encrypted keys for <b>${esc(b.dataset.disc)}</b> will be permanently wiped.</div>`,confirmText:"Disconnect",danger:true,onConfirm:async()=>{await api("/api/exchange/disconnect",{method:"POST",body:{exchange:b.dataset.disc}});toast("Disconnected","ok");refresh();}}));
};
function connectModal(ex,name){
  const needPass=ex==="okx"||ex==="bitget";
  modal("Connect "+name,`<div id="cerr"></div>
    <div class="field"><label>API Key</label><input id="ak" placeholder="API key"></div>
    <div class="field"><label>API Secret</label><input id="as" type="password" placeholder="API secret"></div>
    ${needPass?'<div class="field"><label>Passphrase</label><input id="ap" type="password" placeholder="required for '+name+'"></div>':""}
    <div class="alert warn">Use a <b>trade + futures</b> key with <b>withdrawals disabled</b>. Withdrawal-enabled keys are rejected automatically.</div>
    <button class="btn primary" id="cbtn" style="width:100%">Connect &amp; Validate</button>`, {wide:true});
  $("#cbtn").onclick=()=>withLoading($("#cbtn"), async()=>{
    const body={exchange:ex,api_key:$("#ak").value.trim(),api_secret:$("#as").value.trim()};
    if(needPass)body.passphrase=$("#ap").value.trim();
    if(!body.api_key||!body.api_secret){$("#cerr").innerHTML=`<div class="alert warn">Key and secret required.</div>`;return;}
    try{await api("/api/exchange/connect",{method:"POST",body});closeModal();toast(name+" connected","ok");refresh();}
    catch(e){$("#cerr").innerHTML=`<div class="alert danger">${esc(e.detail)}</div>`;}
  });
}

PAGES.auto = async (v) => {
  const c=await tryGet("/api/auto/config");
  if(c.disabled) return void(v.innerHTML=disabledCard("Auto Trading","AUTO_TRADE_DEMO_ENABLED"));
  if(c.error) return void(v.innerHTML=`<div class="card pad">${empty("⚠️","Could not load",c.error)}</div>`);
  const C=c.data; const st=await tryGet("/api/auto/status"); const S=st.data;
  const safe = S ? (S.global_demo_enabled? '<span class="pos">DEMO OK</span>':'<span class="neg">PAUSED</span>') : "—";
  v.innerHTML=`<div class="alert info">🤖 Auto-trading executes on your <b>paper account only</b> (DEMO). No real orders are ever placed.</div>
    <div class="kpis">
      ${stat("Auto Enabled", S&&S.enabled?'<span class="pos">ON</span>':'<span class="neg">OFF</span>', "your engine","🤖")}
      ${stat("Total Opened", S?S.total_opened:"—", "open now "+(S?S.open_auto_positions:"—"),"📂")}
      ${stat("Total Skipped", S?S.total_skipped:"—", "filtered signals","⏭️")}
      ${stat("Safety Status", safe, "global demo engine","🛡️")}
    </div>
    <div class="grid g2 mt">
      <div class="card"><div class="card-h"><h3>Configuration</h3><label class="switch"><input type="checkbox" id="en" ${C.enabled?"checked":""}><span class="sl"></span></label></div>
        <div class="card-b">
          <div class="form-grid">
            <div class="field"><label>Risk per trade (%)</label><input id="risk" type="number" step="0.1" value="${C.risk_per_trade_pct}"></div>
            <div class="field"><label>Min confidence</label><input id="minc" type="number" value="${C.min_confidence}"></div>
            <div class="field"><label>Max positions</label><input id="maxp" type="number" value="${C.max_positions}"></div>
            <div class="field"><label>Max leverage</label><input id="maxl" type="number" value="${C.max_leverage}"></div>
            <div class="field"><label>Break-even</label><select id="be"><option value="true" ${C.use_break_even?"selected":""}>On</option><option value="false" ${!C.use_break_even?"selected":""}>Off</option></select></div>
            <div class="field"><label>BE trigger</label><select id="bet"><option ${C.break_even_trigger==="TP1"?"selected":""}>TP1</option><option ${C.break_even_trigger==="TP2"?"selected":""}>TP2</option></select></div>
          </div>
          <div class="field"><label>Allowed coins (CSV, blank = all)</label><input id="coins" value="${esc(C.allowed_coins)}" placeholder="BTC,ETH,SOL"></div>
          <button class="btn primary" id="save">Save Configuration</button>
        </div></div>
      <div class="card"><div class="card-h"><h3>Engine Status</h3></div><div class="card-b" id="autostat">${S?
        `<div class="kv"><span>Your auto-trade</span>${S.enabled?badge("ACTIVE"):badge("OFF","muted")}</div>
         <div class="kv"><span>Global demo engine</span>${S.global_demo_enabled?badge("OK"):badge("OFF","muted")}</div>
         <div class="kv"><span>Open auto positions</span><b>${S.open_auto_positions}</b></div>
         <div class="kv"><span>Total opened</span><b>${S.total_opened}</b></div>
         <div class="kv"><span>Total closed</span><b>${S.total_closed}</b></div>
         <div class="kv"><span>Total skipped</span><b>${S.total_skipped}</b></div>`:empty("—","No status")}</div></div>
    </div>
    <div class="card mt"><div class="card-h"><h3>Execution History</h3></div><div id="exec">${skel(4)}</div></div>`;
  const save=(btn,patch)=>withLoading(btn, async()=>{try{await api("/api/auto/config",{method:"PUT",body:patch});toast("Saved","ok");}catch(e){toast(e.detail,"bad");}});
  $("#en").onchange=e=>save(null,{enabled:e.target.checked});
  $("#save").onclick=()=>save($("#save"),{risk_per_trade_pct:+$("#risk").value,max_positions:+$("#maxp").value,max_leverage:+$("#maxl").value,min_confidence:+$("#minc").value,allowed_coins:$("#coins").value.trim(),use_break_even:$("#be").value==="true",break_even_trigger:$("#bet").value});
  const ex=await tryGet("/api/auto/executions");const rows=ex.data||[];
  $("#exec").innerHTML=rows.length?tableWrap(["When","Symbol","Action","Reason","Detail"],
    rows.slice(0,50).map(r=>`<tr><td class="num">${ago(r.created_at)} ago</td><td><b>${esc(r.symbol)}</b></td><td>${badge(r.action,"muted")}</td><td>${esc(r.reason)}</td><td class="sub">${esc(r.detail||"")}</td></tr>`).join("")):empty("📭","No executions yet","Auto-trade decisions will be logged here.");
};

PAGES.safety = async (v) => {
  const s=await tryGet("/api/safety/status");
  if(s.disabled) return void(v.innerHTML=disabledCard("Safety Layer","SAFETY_LAYER_ENABLED"));
  if(s.error) return void(v.innerHTML=`<div class="card pad">${empty("⚠️","Could not load",s.error)}</div>`);
  const S=s.data; const c=await tryGet("/api/safety/config"); const C=c.data||{};
  const blocked=!S.trading_enabled;
  v.innerHTML=`${blocked?`<div class="alert danger">⛔ Trading is currently blocked: ${S.global_kill?"GLOBAL emergency stop":S.kill_switch?"your kill switch is ON":S.disabled_reason||"locked out"}.</div>`:`<div class="alert ok">✅ Trading enabled — all safety checks passing.</div>`}
    <div class="kpis">
      ${stat("Daily PnL",`<span class="${cls(S.daily_pnl)}">${money(S.daily_pnl)}</span>`,"limit "+num(S.max_daily_loss_pct,0)+"%","📅")}
      ${stat("Weekly PnL",`<span class="${cls(S.weekly_pnl)}">${money(S.weekly_pnl)}</span>`,"limit "+num(S.max_weekly_loss_pct,0)+"%","🗓️")}
      ${stat("Loss Streak",S.loss_streak,"consecutive losses","📉")}
      ${stat("Status",blocked?'<span class="neg">BLOCKED</span>':'<span class="pos">ACTIVE</span>',S.disabled_until?("until "+ago(S.disabled_until)):"","🚦")}
    </div>
    <div class="grid g2 mt">
      <div class="danger-zone">
        <div class="dz-h">🛑 <h3>Danger Zone — Kill Switches</h3></div>
        <div class="dz-b">
          <div class="kv"><span>${dot(!S.global_kill)} Global emergency stop</span>${S.global_kill?badge("LIVE"):badge("OK")}</div>
          <div class="kv"><span>${dot(!S.kill_switch)} Your kill switch</span>${S.kill_switch?badge("SUSPENDED"):badge("OK")}</div>
          <div class="legend">
            <span>${dot(true)} Green = trading allowed</span>
            <span>${dot(false)} Red = trading blocked</span>
          </div>
          <div class="row" style="margin-top:16px">
            ${S.kill_switch?'<button class="btn primary" id="resume">Resume Trading</button>':'<button class="btn danger" id="kill">🛑 Activate Kill Switch</button>'}</div>
          <div class="sub" style="margin-top:10px">Your kill switch immediately halts your auto-trading and blocks new positions until you resume.</div>
        </div></div>
      <div class="card"><div class="card-h"><h3>Limits</h3></div><div class="card-b">
        <div class="form-grid">
          <div class="field"><label>Max daily loss %</label><input id="dl" type="number" value="${C.max_daily_loss_pct}"></div>
          <div class="field"><label>Max weekly loss %</label><input id="wl" type="number" value="${C.max_weekly_loss_pct}"></div>
          <div class="field"><label>Max open</label><input id="mo" type="number" value="${C.max_open_positions}"></div>
          <div class="field"><label>Max correlated</label><input id="mc" type="number" value="${C.max_correlated_positions}"></div>
        </div>
        <div class="field"><label>Loss-streak limit</label><input id="ls" type="number" value="${C.loss_streak_limit}"></div>
        <button class="btn primary" id="savelim">Save Limits</button>
      </div></div>
    </div>`;
  $("#kill")&&($("#kill").onclick=()=>confirmModal({title:"Activate kill switch",body:`<div class="alert danger">⛔ This immediately stops your auto-trading and blocks all new positions. You can resume manually at any time.</div>`,confirmText:"🛑 Activate Kill Switch",danger:true,onConfirm:async()=>{await api("/api/safety/kill",{method:"POST"});toast("Kill switch ON","warn");refresh();}}));
  $("#resume")&&($("#resume").onclick=()=>withLoading($("#resume"), async()=>{try{await api("/api/safety/resume",{method:"POST"});toast("Trading resumed","ok");refresh();}catch(e){toast(e.detail,"bad");}}));
  $("#savelim").onclick=()=>withLoading($("#savelim"), async()=>{try{await api("/api/safety/config",{method:"PUT",body:{max_daily_loss_pct:+$("#dl").value,max_weekly_loss_pct:+$("#wl").value,max_open_positions:+$("#mo").value,max_correlated_positions:+$("#mc").value,loss_streak_limit:+$("#ls").value}});toast("Limits saved","ok");}catch(e){toast(e.detail,"bad");}});
};

PAGES.live = async (v) => {
  const g=await cachedGet("/api/live/status");
  if(g.disabled) return void(v.innerHTML=disabledCard("Live Trading","LIVE_TRADING_API_ENABLED"));
  if(g.error) return void(v.innerHTML=`<div class="card pad">${empty("⚠️","Could not load",g.error)}</div>`);
  const G=g.data;
  const banner=G.live_gate_open
    ? `<div class="alert danger">⚡ <b>LIVE GATE OPEN</b> — real orders can be placed. (LIVE_TRADING_ENABLED on &amp; MOCK off)</div>`
    : `<div class="alert ok">🧪 <b>MOCK MODE</b> — all execution is simulated. No real orders are placed. The live gate requires <b>LIVE_TRADING_ENABLED=true</b> AND <b>MOCK_EXCHANGE_MODE=false</b>.</div>`;
  v.innerHTML=`${banner}
    <div class="row" style="margin:0 0 16px"><span class="badge ${G.live_gate_open?"LIVE":"MOCK"} lg">${G.mode} MODE</span>
      <span class="badge muted" style="align-self:center">live_enabled ${G.live_trading_enabled}</span><span class="badge muted" style="align-self:center">mock ${G.mock_exchange_mode}</span></div>
    <div class="tabs"><button class="active" data-t="pos">Positions</button><button data-t="ord">Orders</button><button data-t="trd">Trades</button></div>
    <div id="ltab">${skel(4)}</div>`;
  const tabs=v.querySelectorAll(".tabs button");
  const load=async(t)=>{tabs.forEach(b=>b.classList.toggle("active",b.dataset.t===t));const box=$("#ltab");box.innerHTML=`<div class="card">${skel(4)}</div>`;
    if(t==="pos"){const p=await tryGet("/api/live/positions");const r=p.data||[];
      box.innerHTML=`<div class="card">${r.length?tableWrap(["Mode","Exchange","Symbol","Side","Qty","Entry","Lev","Status","PnL"],
        r.map(x=>`<tr><td>${badge(x.mode)}</td><td>${esc(x.exchange)}</td><td><b>${esc(x.symbol)}</b></td><td>${badge(x.side)}</td><td class="num">${num(x.quantity,4)}</td><td class="num">${num(x.entry_price,4)}</td><td>${x.leverage}x</td><td>${badge(x.status)}</td><td class="num ${cls(x.realized_pnl)}">${money(x.realized_pnl)}</td></tr>`).join("")):empty("📭","No live positions","Positions opened in MOCK or LIVE mode appear here.")}</div>`;
    } else if(t==="ord"){const p=await tryGet("/api/live/orders");const r=p.data||[];
      box.innerHTML=`<div class="card">${r.length?tableWrap(["Mode","Exchange","Symbol","Side","Type","Qty","Status","When"],
        r.map(x=>`<tr><td>${badge(x.mode)}</td><td>${esc(x.exchange)}</td><td><b>${esc(x.symbol)}</b></td><td>${badge(x.side)}</td><td>${esc(x.order_type)}</td><td class="num">${num(x.quantity,4)}</td><td>${badge(x.status)}</td><td class="num">${ago(x.created_at)} ago</td></tr>`).join("")):empty("📭","No orders","Submitted orders (simulated in MOCK) appear here.")}</div>`;
    } else {const p=await tryGet("/api/live/trades");const r=p.data||[];
      box.innerHTML=`<div class="card">${r.length?tableWrap(["Mode","Symbol","Side","Entry","Exit","PnL","When"],
        r.map(x=>`<tr><td>${badge(x.mode)}</td><td><b>${esc(x.symbol)}</b></td><td>${badge(x.side)}</td><td class="num">${num(x.entry_price,4)}</td><td class="num">${num(x.exit_price,4)}</td><td class="num ${cls(x.pnl_usdt)}">${money(x.pnl_usdt)}</td><td class="num">${ago(x.closed_at)} ago</td></tr>`).join("")):empty("📭","No trades","Closed live/MOCK trades appear here.")}</div>`;
    }};
  tabs.forEach(b=>b.onclick=()=>load(b.dataset.t));load("pos");
};

PAGES.profile = async (v) => {
  const me=await tryGet("/api/auth/me"); if(me.data)ME=me.data;
  const U=ME;
  v.innerHTML=`<div class="grid g2">
    <div class="card pad"><div class="row" style="align-items:center;gap:16px"><div class="avatar" style="width:56px;height:56px;font-size:22px">${esc((U.email||"?")[0].toUpperCase())}</div>
      <div><div style="font-size:18px;font-weight:800">${esc(U.username||U.email.split("@")[0])}</div><div class="sub">${esc(U.email)}</div><div style="margin-top:6px">${badge(U.role)} ${U.is_verified?badge("OK"):badge("PENDING")}</div></div></div>
      <div class="kv" style="margin-top:16px"><span>Account ID</span><b>#${U.id}</b></div>
      <div class="kv"><span>Member since</span><b>${when(U.created_at)}</b></div>
      <div class="kv"><span>Last login</span><b>${when(U.last_login_at)}</b></div>
      <div class="kv"><span>2FA</span><b>${U.totp_enabled?'<span class="pos">Enabled</span>':'<span class="neg">Disabled</span>'}</b></div>
    </div>
    <div class="card"><div class="card-h"><h3>Active Sessions</h3></div><div id="sess">${skel(3)}</div></div></div>
    <div class="card mt"><div class="card-h"><h3>Security Recommendations</h3></div><div class="card-b">
      <ul class="recs">
        <li><span class="ic">${U.totp_enabled?"✓":"⚠"}</span><div><b>Enable two-factor authentication (2FA).</b> ${U.totp_enabled?"2FA is active on your account.":"Add an authenticator app for an extra layer of protection."}</div></li>
        <li><span class="ic">🔑</span><div><b>Never share your API keys.</b> Alpha Radar staff will never ask for exchange keys or passwords.</div></li>
        <li><span class="ic">🚫</span><div><b>Use exchange keys without withdrawal permission.</b> Trade + futures only — withdrawal-enabled keys are rejected by the vault.</div></li>
      </ul>
    </div></div>`;
  const ss=await tryGet("/api/auth/sessions");const rows=ss.data||[];
  $("#sess").innerHTML=rows.length?tableWrap(["Device","IP","Last seen",""],
    rows.map(r=>`<tr><td class="sub">${esc((r.device||"unknown").slice(0,40))}</td><td class="num">${esc(maskIp(r.ip))}</td><td class="num">${ago(r.last_seen)} ago</td><td>${r.current?badge("ACTIVE"):""}</td></tr>`).join("")):empty("—","No active sessions");
};

PAGES.admin = async (v) => {
  if(!ME||ME.role!=="ADMIN") return void(v.innerHTML=`<div class="card pad">${empty("🔒","Admins only","Your role: "+(ME?ME.role:"?"))}</div>`);
  const o=await tryGet("/api/admin/overview");
  if(o.disabled) return void(v.innerHTML=disabledCard("Admin Platform","ADMIN_DASHBOARD_ENABLED"));
  if(o.error) return void(v.innerHTML=`<div class="card pad">${empty("⚠️","Could not load",o.error)}</div>`);
  const O=o.data;
  v.innerHTML=`<div class="kpis">
      ${stat("Total Users",O.users.total,Object.entries(O.users.by_role).map(([k,n])=>k+" "+n).join(" · "),"👥")}
      ${stat("Connected Exch",O.exchange_accounts.connected,Object.keys(O.exchange_accounts.by_exchange||{}).join(", ")||"none","🔗")}
      ${stat("Open Positions",O.positions.open_total,"LIVE "+O.positions.open_live+" · MOCK "+O.positions.open_mock,"📂")}
      ${stat("Realized PnL",`<span class="${cls(O.realized_pnl_usdt)}">${money(O.realized_pnl_usdt)}</span>`,"all live trades","📈")}
      ${stat("Auto Users",O.auto_trading_enabled_users,"","🤖")}
      ${stat("Kill Switches",O.user_kill_switches_active,"active","🛑")}
      ${stat("Global Kill",O.global_kill?'<span class="neg">ON</span>':'<span class="pos">OFF</span>',"","⛔")}
      ${stat("Live Gate",O.live_gate_open?'<span class="neg">OPEN</span>':'<span class="pos">CLOSED</span>',O.live_gate_open?"real orders possible":"mock only","⚡")}
    </div>
    <div class="tabs" style="margin-top:16px"><button class="active" data-t="users">Users</button><button data-t="audit">Audit Log</button></div>
    <div id="atab">${skel(5)}</div>`;
  const tabs=v.querySelectorAll(".tabs button");
  const load=async(t)=>{tabs.forEach(b=>b.classList.toggle("active",b.dataset.t===t));const box=$("#atab");box.innerHTML=`<div class="card">${skel(5)}</div>`;
    if(t==="users"){const u=await tryGet("/api/admin/users?limit=200");const rows=(u.data&&u.data.users)||[];
      box.innerHTML=`<div class="card">${rows.length?tableWrap(["ID","Email","Role","Status","Exch","Auto","Kill","Last login",""],
        rows.map(r=>`<tr><td>${r.id}</td><td>${esc(r.email)}</td><td>${badge(r.role)}</td><td>${badge(r.status)}</td><td class="num">${r.connected_exchanges}</td>
          <td>${dot(r.auto_trading)}</td><td>${r.kill_switch?'<span class="neg">●</span>':dot(false)}</td><td class="num">${r.last_login_at?ago(r.last_login_at)+" ago":"—"}</td>
          <td><button class="btn sm" data-u="${r.id}">View</button> ${r.status==="SUSPENDED"?`<button class="btn sm" data-act="${r.id}">Activate</button>`:`<button class="btn sm danger" data-sus="${r.id}">Suspend</button>`}</td></tr>`).join("")):empty("—","No users")}</div>`;
      box.querySelectorAll("[data-u]").forEach(b=>b.onclick=()=>adminUser(b.dataset.u));
      box.querySelectorAll("[data-sus]").forEach(b=>b.onclick=()=>adminStatus(b.dataset.sus,"SUSPENDED"));
      box.querySelectorAll("[data-act]").forEach(b=>b.onclick=()=>adminStatus(b.dataset.act,"ACTIVE"));
    } else {const a=await tryGet("/api/admin/audit?limit=80");const rows=a.data||[];
      box.innerHTML=`<div class="card">${rows.length?tableWrap(["When","User","Exchange","Symbol","Action","Result","Mode"],
        rows.map(r=>`<tr><td class="num">${ago(r.created_at)} ago</td><td>${r.user_id??"—"}</td><td>${esc(r.exchange)}</td><td>${esc(r.symbol||"—")}</td><td>${esc(r.action)}</td><td>${badge(r.result)}</td><td>${badge(r.mode)}</td></tr>`).join("")):empty("📭","No audit rows")}</div>`;
    }};
  tabs.forEach(b=>b.onclick=()=>load(b.dataset.t));load("users");
};
async function adminUser(id){try{const d=await api("/api/admin/users/"+id);const p=d.profile;
  modal(p.email, `<div class="kv"><span>Role / Status</span><span>${badge(p.role)} ${badge(p.status)}</span></div>
    <div class="kv"><span>Verified / 2FA</span><span>${dot(p.is_verified)} / ${dot(p.totp_enabled)}</span></div>
    <div class="kv"><span>Created</span><b>${when(p.created_at)}</b></div>
    <h4 style="margin:16px 0 6px">Exchange Accounts</h4>${d.exchange_accounts.length?d.exchange_accounts.map(a=>`<div class="kv"><span>${esc(a.exchange)} ${badge(a.status)}</span><span class="sub">••••${esc(a.api_key_last4||"????")}</span></div>`).join(""):'<div class="sub">None</div>'}
    <h4 style="margin:16px 0 6px">Open Positions (${d.open_positions.length})</h4>${d.open_positions.map(o=>`<div class="kv"><span>${esc(o.symbol)} ${badge(o.side)} ${badge(o.mode)}</span><span class="sub">${num(o.quantity,4)} @ ${num(o.entry_price,4)}</span></div>`).join("")||'<div class="sub">None</div>'}`, {wide:true});
}catch(e){toast(e.detail,"bad");}}
async function adminStatus(id,status){confirmModal({title:status+" user "+id,body:`<div class="alert ${status==="SUSPENDED"?"warn":"info"}">${status==="SUSPENDED"?"Suspending blocks this user from trading.":"Re-activate this user's account."}</div>`,confirmText:status==="SUSPENDED"?"Suspend":"Activate",danger:status==="SUSPENDED",onConfirm:async()=>{await api("/api/admin/users/"+id+"/status",{method:"PUT",body:{status}});toast("User "+status,"ok");refresh();}});}

// ── router (debounced; tears down previous page) ───────────────────
let ROUTE_PENDING=false, ROUTE_TOKEN=0;
async function route(){
  if(!ME)return;
  let r=(location.hash||"#/dashboard").replace("#/","");
  if(!PAGES[r])r="dashboard";
  // teardown previous page: stop its timers + destroy charts (Phase 12)
  clearPageTimers(); destroyCharts();
  setActive(r);
  const token=++ROUTE_TOKEN;
  const v=$("#view"); if(!v)return;
  v.innerHTML=skel(6);
  // re-trigger page transition
  v.style.animation="none"; void v.offsetWidth; v.style.animation="fade .22s ease";
  try{ await PAGES[r](v); }
  catch(e){ if(token!==ROUTE_TOKEN)return; if(e.status===401){logout();return;} v.innerHTML=`<div class="card pad">${empty("⚠️","Failed to load page",e.detail||"")}</div>`; }
}
// debounce rapid hashchange bursts
let HASH_T=null;
window.addEventListener("hashchange", ()=>{ clearTimeout(HASH_T); HASH_T=setTimeout(route, 30); });

// ── boot ───────────────────────────────────────────────────────────
async function boot(){
  if(!TK.a){ renderLanding(); return; }
  try{ ME = await api("/api/auth/me"); }
  catch(e){ TK.clear(); renderLanding(); return; }
  renderShell();
  if(!location.hash||location.hash==="#") location.hash="#/dashboard";
  route();
}
boot();
})();
