"""Manual e2e for Sprint 20A auth — run inside container with Postgres up.

Not part of the pytest suite (requires a live DB). Exercises the real ASGI
app end-to-end via httpx.
"""
import asyncio

import httpx

from app.dashboard import create_app
from app.database.session import init_db


async def main() -> None:
    await init_db()
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        email = "trader@example.com"
        r = await c.post("/api/auth/register", json={"email": email, "password": "Sup3rSecret!"})
        print("register", r.status_code, r.json())
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "ADMIN"  # first user

        # duplicate
        r = await c.post("/api/auth/register", json={"email": email, "password": "Sup3rSecret!"})
        print("dup-register", r.status_code, r.json())
        assert r.status_code == 409

        # wrong password
        r = await c.post("/api/auth/login", json={"email": email, "password": "nope"})
        print("bad-login", r.status_code, r.json())
        assert r.status_code == 401

        # good login
        r = await c.post("/api/auth/login", json={"email": email, "password": "Sup3rSecret!"})
        print("login", r.status_code)
        assert r.status_code == 200, r.text
        tokens = r.json()
        access, refresh = tokens["access_token"], tokens["refresh_token"]

        # /me without token -> 401
        r = await c.get("/api/auth/me")
        print("me-noauth", r.status_code)
        assert r.status_code == 401

        # /me with token
        r = await c.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
        print("me", r.status_code, r.json()["email"])
        assert r.status_code == 200 and r.json()["email"] == email

        # refresh
        r = await c.post("/api/auth/refresh", json={"refresh_token": refresh})
        print("refresh", r.status_code)
        assert r.status_code == 200

        # logout then refresh should fail
        r = await c.post("/api/auth/logout", json={"refresh_token": refresh})
        print("logout", r.status_code)
        r = await c.post("/api/auth/refresh", json={"refresh_token": refresh})
        print("refresh-after-logout", r.status_code)
        assert r.status_code == 401

    print("E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
