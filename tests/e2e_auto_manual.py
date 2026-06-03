"""Manual e2e for Sprint 20D auto engine (DEMO) — run inside container w/ Postgres.

Drives the real server-side engine (on_new_signal / on_signal_event) plus the
HTTP config/status API: auto-open from a signal, break-even on TP1, close on SL
at break-even (~0 PnL), and a SKIP via the allowed-coins filter.
"""

import asyncio

import httpx

from app.auto_engine.engine import on_new_signal, on_signal_event
from app.dashboard import create_app
from app.database.models import Signal
from app.database.session import get_session, init_db


async def _seed_signal(symbol: str) -> int:
    async with get_session() as db:
        sig = Signal(
            symbol=symbol,
            side="LONG",
            timeframe="1h",
            confidence=90.0,
            risk_level="LOW",
            strategy="MTF_SMC_STRICT",
            reasons="test",
            entry_low=100.0,
            entry_high=100.0,
            tp1=104.0,
            tp2=108.0,
            tp3=112.0,
            stop_loss=98.0,
            risk_reward=2.0,
            # CLOSED keeps the seed out of the uq_active_signal_symbol partial
            # index so re-runs on the shared dev DB don't collide; the auto
            # engine acts on the signal regardless of its status.
            status="CLOSED",
        )
        db.add(sig)
        await db.flush()
        return sig.id


async def _purge() -> None:
    """Idempotent cleanup so the e2e is repeatable on the shared dev DB."""
    from sqlalchemy import delete, select

    from app.database.models import AuthUser

    async with get_session() as db:
        u = (
            await db.execute(select(AuthUser).where(AuthUser.email == "auto@example.com"))
        ).scalar_one_or_none()
        if u:
            await db.delete(u)
        await db.execute(
            delete(Signal).where(Signal.strategy == "MTF_SMC_STRICT", Signal.reasons == "test")
        )


async def main() -> None:
    await init_db()
    await _purge()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/auth/register", json={"email": "auto@example.com", "password": "Sup3rSecret!"}
        )
        r = await c.post(
            "/api/auth/login", json={"email": "auto@example.com", "password": "Sup3rSecret!"}
        )
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # enable auto-trade for BTC only, with break-even on TP1
        r = await c.put(
            "/api/auto/config",
            headers=h,
            json={
                "enabled": True,
                "allowed_coins": "BTC",
                "use_break_even": True,
                "break_even_trigger": "TP1",
                "max_leverage": 10,
                "risk_per_trade_pct": 1.0,
            },
        )
        print("config", r.status_code, r.json()["enabled"], r.json()["allowed_coins"])
        assert r.status_code == 200 and r.json()["enabled"]

        # --- SKIP path: ETH signal filtered out by allowed_coins=BTC ---
        eth_id = await _seed_signal("ETHUSDT")
        opened = await on_new_signal(eth_id)
        print("eth on_new_signal opened =", opened)
        assert opened == 0

        # --- OPEN path: BTC signal ---
        btc_id = await _seed_signal("BTCUSDT")
        opened = await on_new_signal(btc_id)
        print("btc on_new_signal opened =", opened)
        assert opened == 1

        # idempotency: same signal again opens nothing
        assert await on_new_signal(btc_id) == 0

        # paper account shows 1 auto-managed open position
        r = await c.get("/api/paper/account/positions?status=open", headers=h)
        pos = r.json()
        print("open positions", len(pos), pos[0]["symbol"], "sl=", pos[0]["stop_loss"])
        assert len(pos) == 1 and pos[0]["symbol"] == "BTCUSDT"
        assert pos[0]["stop_loss"] == 98.0

        # status reflects 1 opened
        r = await c.get("/api/auto/status", headers=h)
        print("status", r.json())
        assert r.json()["total_opened"] == 1 and r.json()["open_auto_positions"] == 1

        # --- TP1 -> break-even moves stop to entry (100) ---
        await on_signal_event(btc_id, "TP1")
        r = await c.get("/api/paper/account/positions?status=open", headers=h)
        print("after TP1 sl=", r.json()[0]["stop_loss"])
        assert r.json()[0]["stop_loss"] == 100.0  # break-even at entry

        # --- SL -> closes at break-even stop (100 = entry) => ~0 PnL ---
        await on_signal_event(btc_id, "SL")
        r = await c.get("/api/paper/account/positions?status=open", headers=h)
        assert len(r.json()) == 0  # closed
        r = await c.get("/api/paper/account/trades", headers=h)
        print("trade pnl", r.json()[0]["pnl_usdt"], "reason", r.json()[0]["reason"])
        assert abs(r.json()[0]["pnl_usdt"]) < 0.01  # break-even => ~0, not a loss

        # account balance unchanged (break-even saved the trade)
        r = await c.get("/api/paper/account/", headers=h)
        print("balance", r.json()["balance"])
        assert abs(r.json()["balance"] - 10000.0) < 0.01

        # executions log shows OPEN, BREAK_EVEN, CLOSE (+ SKIP for ETH)
        r = await c.get("/api/auto/executions", headers=h)
        actions = sorted({e["action"] for e in r.json()})
        print("exec actions", actions)
        assert {"OPEN", "BREAK_EVEN", "CLOSE", "SKIP"}.issubset(set(actions))

    print("AUTO E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
