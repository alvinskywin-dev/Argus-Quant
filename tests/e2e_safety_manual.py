"""Manual e2e for Sprint 20E safety layer — run inside container w/ Postgres.

Proves: correlated-position cap, loss-streak lockout, resume, user kill switch,
and admin GLOBAL emergency stop — all gating the demo auto engine.
"""
import asyncio

import httpx

from app.auto_engine.engine import on_new_signal, on_signal_event
from app.dashboard import create_app
from app.database.models import Signal
from app.database.session import get_session, init_db


async def _seed(symbol: str, side: str = "LONG") -> int:
    async with get_session() as db:
        sig = Signal(
            symbol=symbol, side=side, timeframe="1h", confidence=90.0,
            risk_level="LOW", strategy="MTF_SMC_STRICT", reasons="test",
            entry_low=100.0, entry_high=100.0, tp1=104.0, tp2=108.0, tp3=112.0,
            stop_loss=98.0, risk_reward=2.0,
            # CLOSED keeps these out of the active-signal unique index so the
            # same symbol can be reused across phases; the engine ignores status.
            status="CLOSED",
        )
        db.add(sig)
        await db.flush()
        return sig.id


async def _register(c, email):
    await c.post("/api/auth/register", json={"email": email, "password": "Sup3rSecret!"})
    r = await c.post("/api/auth/login", json={"email": email, "password": "Sup3rSecret!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _promote(email):
    """Force ADMIN role (role is read from the DB per request)."""
    from sqlalchemy import select
    from app.database.models import AuthUser
    async with get_session() as db:
        u = (await db.execute(
            select(AuthUser).where(AuthUser.email == email.lower()))).scalar_one()
        u.role = "ADMIN"


async def _open_count(c, h) -> int:
    r = await c.get("/api/paper/account/positions?status=open", headers=h)
    return len(r.json())


async def _has_skip(c, h, needle: str) -> bool:
    r = await c.get("/api/auto/executions", headers=h)
    return any(e["action"] == "SKIP" and needle in (e["detail"] or "") for e in r.json())


async def _purge(emails) -> None:
    from sqlalchemy import delete, select
    from app.database.models import AuthUser, Signal, SystemSetting
    async with get_session() as db:
        for email in emails:
            # auth stores emails lowercased
            u = (await db.execute(
                select(AuthUser).where(AuthUser.email == email.lower())
            )).scalar_one_or_none()
            if u:
                await db.delete(u)
        gk = await db.get(SystemSetting, "trading_global_kill")
        if gk:
            await db.delete(gk)
        await db.execute(
            delete(Signal).where(Signal.strategy == "MTF_SMC_STRICT", Signal.reasons == "test")
        )


async def main() -> None:
    await init_db()
    # Shared dev DB — start from a clean slate (idempotent across re-runs).
    await _purge(["safeA@example.com", "safeB@example.com"])
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        hA = await _register(c, "safeA@example.com")
        hB = await _register(c, "safeB@example.com")    # FREE
        # Promote A to ADMIN explicitly — on the shared dev DB the first-ever
        # account (not safeA) holds ADMIN, so don't rely on registration order.
        await _promote("safeA@example.com")

        # auto config: BE off so SL is a real loss; all coins; high pos cap
        auto = {"enabled": True, "use_break_even": False, "max_positions": 10,
                "allowed_coins": "", "risk_per_trade_pct": 1.0}
        await c.put("/api/auto/config", headers=hA, json=auto)
        await c.put("/api/auto/config", headers=hB, json={**auto, "enabled": False})

        # ---------- Phase 1: correlated cap (user A only) ----------
        # Assert on user A's own open positions (on_new_signal is multi-user).
        rc = await c.put("/api/safety/config", headers=hA, json={
            "max_correlated_positions": 1, "loss_streak_limit": 50,
            "max_daily_loss_pct": 99, "max_weekly_loss_pct": 99})
        assert rc.status_code == 200, rc.text
        assert rc.json()["max_correlated_positions"] == 1
        await on_new_signal(await _seed("BTCUSDT"))                 # MAJOR long -> opens
        assert await _open_count(c, hA) == 1
        await on_new_signal(await _seed("ETHUSDT"))                 # MAJOR long -> blocked
        assert await _open_count(c, hA) == 1, "correlated MAJOR should be blocked"
        await on_new_signal(await _seed("SOLUSDT"))                 # L1 -> allowed
        assert await _open_count(c, hA) == 2
        assert await _has_skip(c, hA, "correlated"), "expected a max-correlated SKIP"
        print("correlated cap OK — A open positions:", await _open_count(c, hA))
        # turn A off for the rest so shared signals don't open for A
        await c.put("/api/auto/config", headers=hA, json={"enabled": False})

        # ---------- Phase 2: loss-streak lockout (user B) ----------
        await c.put("/api/auto/config", headers=hB, json={"enabled": True})
        rc = await c.put("/api/safety/config", headers=hB, json={
            "loss_streak_limit": 2, "max_daily_loss_pct": 99, "max_weekly_loss_pct": 99,
            "max_correlated_positions": 99})
        assert rc.status_code == 200, rc.text
        assert rc.json()["loss_streak_limit"] == 2

        for sym in ("BTCUSDT", "ETHUSDT"):
            sid = await _seed(sym)
            await on_new_signal(sid)
            assert await _open_count(c, hB) == 1
            await on_signal_event(sid, "SL")     # close at 98 -> ~-100 loss
            assert await _open_count(c, hB) == 0

        r = await c.get("/api/safety/status", headers=hB)
        print("after 2 losses:", "enabled=", r.json()["trading_enabled"],
              "streak=", r.json()["loss_streak"], "daily=", r.json()["daily_pnl"])
        assert r.json()["loss_streak"] == 2

        # 3rd signal must be blocked by loss-streak protection (no position opens)
        await on_new_signal(await _seed("SOLUSDT"))
        assert await _open_count(c, hB) == 0
        r = await c.get("/api/safety/status", headers=hB)
        print("locked:", r.json()["trading_enabled"], r.json()["disabled_reason"])
        assert r.json()["trading_enabled"] is False
        assert "losses in a row" in (r.json()["disabled_reason"] or "")
        assert await _has_skip(c, hB, "loss streak")

        # resume clears the lockout (a winning trade would also reset the streak)
        await c.post("/api/safety/resume", headers=hB)
        r = await c.get("/api/safety/status", headers=hB)
        print("after resume:", r.json()["trading_enabled"])
        assert r.json()["trading_enabled"] is True

        # ---------- Phase 3: user kill switch ----------
        await c.post("/api/safety/kill", headers=hB)
        await on_new_signal(await _seed("XRPUSDT"))
        assert await _open_count(c, hB) == 0
        r = await c.get("/api/safety/status", headers=hB)
        print("user kill:", r.json()["kill_switch"], r.json()["trading_enabled"])
        assert r.json()["kill_switch"] and not r.json()["trading_enabled"]
        await c.post("/api/safety/resume", headers=hB)

        # ---------- Phase 4: admin GLOBAL emergency stop ----------
        r = await c.post("/api/admin/safety/kill-all", headers=hA)
        print("admin kill-all:", r.status_code, r.json()["detail"])
        assert r.status_code == 200
        # FREE user cannot trigger global kill
        assert (await c.post("/api/admin/safety/kill-all", headers=hB)).status_code == 403
        # global stop blocks even a clean signal
        await on_new_signal(await _seed("ADAUSDT"))
        assert await _open_count(c, hB) == 0
        r = await c.get("/api/safety/status", headers=hB)
        assert r.json()["global_kill"] and not r.json()["trading_enabled"]
        await c.post("/api/admin/safety/resume-all", headers=hA)
        r = await c.get("/api/safety/status", headers=hB)
        assert not r.json()["global_kill"]
        print("global resumed:", r.json()["trading_enabled"])

    # self-clean so a re-run starts fresh (this DB is shared across manual e2es)
    await _purge(["safeA@example.com", "safeB@example.com"])

    print("SAFETY E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
