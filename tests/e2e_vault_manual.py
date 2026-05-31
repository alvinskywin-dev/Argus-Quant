"""Manual e2e for Sprint 20C exchange vault — run inside container w/ Postgres.

Validates connect (with permission checks + withdrawal rejection), no-plaintext
storage, test, accounts listing, and disconnect (secret wipe).
"""
import asyncio

import httpx
from sqlalchemy import select

from app.dashboard import create_app
from app.database.models import ExchangeAccount
from app.database.session import get_session, init_db


async def _row(email_user_id: int):
    async with get_session() as db:
        res = await db.execute(select(ExchangeAccount).where(ExchangeAccount.user_id == email_user_id))
        return res.scalar_one_or_none()


async def main() -> None:
    await init_db()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/register", json={"email": "vault@example.com", "password": "Sup3rSecret!"})
        r = await c.post("/api/auth/login", json={"email": "vault@example.com", "password": "Sup3rSecret!"})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        from app.auth.security import decode_access_token
        uid = int(decode_access_token(r.json()["access_token"])["sub"])

        # reject: withdrawal-enabled key is NOT stored
        r = await c.post("/api/exchange/connect", headers=h, json={
            "exchange": "binance", "api_key": "WITHDRAWkey99", "api_secret": "sekret"})
        print("connect-withdrawal", r.status_code, r.json().get("detail"))
        assert r.status_code == 403
        assert await _row(uid) is None, "withdrawal key must NOT be persisted"

        # reject: okx without passphrase
        r = await c.post("/api/exchange/connect", headers=h, json={
            "exchange": "okx", "api_key": "GOODKEY", "api_secret": "sekret"})
        print("connect-okx-nopass", r.status_code, r.json().get("detail"))
        assert r.status_code == 400

        # success: trade+futures-only key
        r = await c.post("/api/exchange/connect", headers=h, json={
            "exchange": "binance", "api_key": "GOODKEYabcd1234", "api_secret": "topsecret"})
        print("connect-ok", r.status_code, r.json())
        assert r.status_code == 201
        assert r.json()["status"] == "CONNECTED"
        assert r.json()["can_withdraw"] is False
        assert r.json()["api_key_last4"] == "1234"
        assert "api_secret" not in r.json() and "encrypted" not in str(r.json())

        # DB holds ciphertext only — never the plaintext key/secret
        row = await _row(uid)
        assert row.encrypted_api_key and "GOODKEYabcd1234" not in row.encrypted_api_key
        assert row.encrypted_api_secret and "topsecret" not in row.encrypted_api_secret
        from app.exchange_vault import crypto
        assert crypto.decrypt(row.encrypted_api_key) == "GOODKEYabcd1234"  # decrypts back
        print("ciphertext-only OK")

        # test connection
        r = await c.post("/api/exchange/test", headers=h, json={"exchange": "binance"})
        print("test", r.status_code, r.json())
        assert r.status_code == 200 and r.json()["status"] == "CONNECTED" and r.json()["can_futures"]

        # accounts listing (no secrets)
        r = await c.get("/api/exchange/accounts", headers=h)
        print("accounts", [(a["exchange"], a["status"], a["api_key_last4"]) for a in r.json()])
        assert len(r.json()) == 1 and "encrypted_api_key" not in r.json()[0]

        # disconnect wipes ciphertext
        r = await c.post("/api/exchange/disconnect", headers=h, json={"exchange": "binance"})
        print("disconnect", r.status_code, r.json())
        assert r.status_code == 200
        row = await _row(uid)
        assert row.status == "DISCONNECTED" and row.encrypted_api_key is None
        print("secret-wiped OK")

    print("VAULT E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
