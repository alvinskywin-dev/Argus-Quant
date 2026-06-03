"""Manual e2e for Sprint 20F live trading (MOCK mode) — run w/ Postgres.

Proves the full pipeline runs in MOCK with NO real orders: vault connect ->
open -> position -> close -> trade/audit, plus the safety kill switch blocking
an open. Asserts every result is mode=MOCK and the live gate is closed.
"""

import asyncio

import httpx
from sqlalchemy import select

from app.dashboard import create_app
from app.database.models import AuthUser, LiveAuditLog
from app.database.session import get_session, init_db


async def _purge(email):
    async with get_session() as db:
        u = (
            await db.execute(select(AuthUser).where(AuthUser.email == email.lower()))
        ).scalar_one_or_none()
        if u:
            await db.delete(u)


async def main() -> None:
    await init_db()
    await _purge("live@example.com")
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/auth/register", json={"email": "live@example.com", "password": "Sup3rSecret!"}
        )
        r = await c.post(
            "/api/auth/login", json={"email": "live@example.com", "password": "Sup3rSecret!"}
        )
        tok = r.json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        from app.auth.security import decode_access_token

        uid = int(decode_access_token(tok)["sub"])

        # gate must be CLOSED (default) -> MOCK
        r = await c.get("/api/live/status")
        print("gate:", r.json())
        assert r.json()["live_gate_open"] is False and r.json()["mode"] == "MOCK"

        # connect a (mock-validated) exchange key via the vault
        r = await c.post(
            "/api/exchange/connect",
            headers=h,
            json={"exchange": "binance", "api_key": "GOODKEYabcd1234", "api_secret": "topsecret"},
        )
        assert r.status_code == 201, r.text

        # balance (mock)
        r = await c.get("/api/live/balance?exchange=binance", headers=h)
        print("balance:", r.json())
        assert r.json()["mode"] == "MOCK"

        # open a position -> MOCK fill, no real order
        r = await c.post(
            "/api/live/open",
            headers=h,
            json={
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "notional_usdt": 5000,
                "entry_price": 50000,
                "leverage": 10,
                "margin_type": "isolated",
                "take_profit": 55000,
                "stop_loss": 48000,
            },
        )
        print("open:", r.status_code, r.json())
        assert r.status_code == 201 and r.json()["mode"] == "MOCK"
        pos_id = r.json()["position_id"]

        r = await c.get("/api/live/positions?status=OPEN", headers=h)
        assert len(r.json()) == 1 and r.json()[0]["mode"] == "MOCK"
        assert r.json()[0]["quantity"] == 0.1  # 5000 / 50000

        # close at +10% -> +500 pnl
        r = await c.post(
            "/api/live/close", headers=h, json={"position_id": pos_id, "exit_price": 55000}
        )
        print("close:", r.status_code, r.json())
        assert r.status_code == 200 and r.json()["pnl_usdt"] == 500.0 and r.json()["mode"] == "MOCK"

        r = await c.get("/api/live/trades", headers=h)
        assert len(r.json()) == 1 and r.json()[0]["pnl_usdt"] == 500.0
        r = await c.get("/api/live/orders", headers=h)
        modes = {o["mode"] for o in r.json()}
        print("orders:", len(r.json()), "modes:", modes)
        assert len(r.json()) == 2 and modes == {"MOCK"}  # open + reduce-only close

        # safety kill switch blocks a new open (403)
        await c.post("/api/safety/kill", headers=h)
        r = await c.post(
            "/api/live/open",
            headers=h,
            json={
                "exchange": "binance",
                "symbol": "ETHUSDT",
                "side": "LONG",
                "notional_usdt": 1000,
                "entry_price": 3000,
                "leverage": 5,
            },
        )
        print("open-while-killed:", r.status_code, r.json().get("detail"))
        assert r.status_code == 403 and "safety" in r.json()["detail"].lower()
        await c.post("/api/safety/resume", headers=h)

        # audit log: OPEN OK, CLOSE OK, OPEN REJECTED — all mode MOCK
        async with get_session() as db:
            rows = (
                (await db.execute(select(LiveAuditLog).where(LiveAuditLog.user_id == uid)))
                .scalars()
                .all()
            )
            audit = sorted({(a.action, a.result) for a in rows})
            assert all(a.mode == "MOCK" for a in rows)
        print("audit:", audit)
        assert (
            ("OPEN", "OK") in audit and ("CLOSE", "OK") in audit and ("OPEN", "REJECTED") in audit
        )

    await _purge("live@example.com")
    print("LIVE E2E OK (all MOCK — no real orders)")


if __name__ == "__main__":
    asyncio.run(main())
