"""Manual e2e for Sprint 20B paper trading — run inside container with Postgres.

Exercises the real ASGI app: register a user, get the demo account, open a
manual position, simulate + copy a signal, close a position, and read history.
"""
import asyncio

import httpx

from app.dashboard import create_app
from app.database.models import Signal
from app.database.session import get_session, init_db


async def _seed_signal() -> int:
    async with get_session() as db:
        sig = Signal(
            symbol="BTCUSDT", side="LONG", timeframe="1h", confidence=90.0,
            risk_level="LOW", strategy="MTF_SMC_STRICT", reasons="test",
            entry_low=100.0, entry_high=100.0, tp1=104.0, tp2=108.0, tp3=112.0,
            stop_loss=98.0, risk_reward=2.0, status="OPEN",
        )
        db.add(sig)
        await db.flush()
        return sig.id


async def main() -> None:
    await init_db()
    signal_id = await _seed_signal()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # register + login
        r = await c.post("/api/auth/register", json={"email": "paper@example.com", "password": "Sup3rSecret!"})
        assert r.status_code == 201, r.text
        r = await c.post("/api/auth/login", json={"email": "paper@example.com", "password": "Sup3rSecret!"})
        assert r.status_code == 200, r.text
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # fresh account = default balance, no positions
        r = await c.get("/api/paper/account/", headers=h)
        acc = r.json()
        print("account", r.status_code, acc["balance"], acc["open_positions"])
        assert r.status_code == 200 and acc["open_positions"] == 0
        start_balance = acc["balance"]

        # simulate from signal (no DB write)
        r = await c.post("/api/paper/account/simulate", headers=h, json={"signal_id": signal_id})
        print("simulate", r.status_code, r.json()["projections"].get("TP1"))
        assert r.status_code == 200 and "TP3" in r.json()["projections"]

        # manual open: LONG 5000 notional @100, 10x
        r = await c.post("/api/paper/account/open", headers=h, json={
            "symbol": "ETHUSDT", "side": "LONG", "entry_price": 100,
            "notional_usdt": 5000, "leverage": 10, "stop_loss": 98,
        })
        print("open", r.status_code, "liq=", r.json().get("liquidation_price"))
        assert r.status_code == 201, r.text
        pos_id = r.json()["id"]
        assert r.json()["margin_usdt"] == 500.0  # 5000 / 10

        # over-leverage rejection: notional needing > available margin
        r = await c.post("/api/paper/account/open", headers=h, json={
            "symbol": "ETHUSDT", "side": "LONG", "entry_price": 100,
            "notional_usdt": 10_000_000, "leverage": 1,
        })
        print("open-too-big", r.status_code, r.json().get("detail"))
        assert r.status_code == 400

        # copy the signal -> opens a BTC position
        r = await c.post("/api/paper/account/copy", headers=h, json={"signal_id": signal_id})
        print("copy", r.status_code, r.json().get("symbol"))
        assert r.status_code == 201, r.text

        # account now shows 2 open + used margin
        r = await c.get("/api/paper/account/", headers=h)
        print("account-2", r.json()["open_positions"], "used_margin=", r.json()["used_margin"])
        assert r.json()["open_positions"] == 2

        # close ETH at +10% (price 110) -> +500 USDT on 5000 notional
        r = await c.post(f"/api/paper/account/positions/{pos_id}/close", headers=h,
                         json={"mark_price": 110, "reason": "MANUAL"})
        print("close", r.status_code, "pnl=", r.json()["pnl_usdt"], "roe=", r.json()["pnl_pct"])
        assert r.status_code == 200 and r.json()["pnl_usdt"] == 500.0

        # balance increased by 500
        r = await c.get("/api/paper/account/", headers=h)
        print("account-3 balance", r.json()["balance"], "daily_pnl", r.json()["daily_pnl"])
        assert r.json()["balance"] == start_balance + 500
        assert r.json()["total_trades"] == 1 and r.json()["win_rate"] == 100.0

        # orders + trades history populated
        r = await c.get("/api/paper/account/orders", headers=h)
        print("orders", len(r.json()))
        assert len(r.json()) >= 3  # 2 opens + 1 close
        r = await c.get("/api/paper/account/trades", headers=h)
        assert len(r.json()) == 1

        # reset wipes everything
        r = await c.post("/api/paper/account/reset", headers=h)
        print("reset", r.json()["balance"], r.json()["open_positions"], r.json()["total_trades"])
        assert r.json()["balance"] == start_balance and r.json()["total_trades"] == 0

    print("PAPER E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
