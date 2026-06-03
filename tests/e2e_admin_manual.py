"""Manual e2e for Sprint 20H Admin Dashboard — run w/ Postgres.

Proves: ADMIN-only access (FREE user gets 403), the overview rollup, user
list/detail (NO decrypted credentials — last4 only), the audit feed, and
suspend/activate moderation incl. the self-suspend guard.

Run:
  docker compose run --rm --no-deps \
    -e AUTH_ENABLED=true -e EMAIL_VERIFICATION_REQUIRED=false \
    -e EXCHANGE_API_VAULT_ENABLED=true -e LIVE_TRADING_API_ENABLED=true \
    -e ADMIN_DASHBOARD_ENABLED=true \
    -v "$(pwd)/app:/app/app" -v "$(pwd)/tests:/app/tests" \
    bot python -m tests.e2e_admin_manual
"""

import asyncio

import httpx
from sqlalchemy import select

from app.dashboard import create_app
from app.database.models import AuthUser
from app.database.session import get_session, init_db

ADMIN_EMAIL = "admin20h@example.com"
USER_EMAIL = "user20h@example.com"


async def _purge(*emails):
    async with get_session() as db:
        for email in emails:
            u = (
                await db.execute(select(AuthUser).where(AuthUser.email == email.lower()))
            ).scalar_one_or_none()
            if u:
                await db.delete(u)


async def _promote(email):
    async with get_session() as db:
        u = (await db.execute(select(AuthUser).where(AuthUser.email == email.lower()))).scalar_one()
        u.role = "ADMIN"
        uid = u.id
    return uid


async def _register_login(c, email):
    await c.post("/api/auth/register", json={"email": email, "password": "Sup3rSecret!"})
    r = await c.post("/api/auth/login", json={"email": email, "password": "Sup3rSecret!"})
    tok = r.json()["access_token"]
    from app.auth.security import decode_access_token

    return tok, int(decode_access_token(tok)["sub"])


async def main() -> None:
    await init_db()
    await _purge(ADMIN_EMAIL, USER_EMAIL)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # An ADMIN (promoted explicitly to be robust on the shared dev DB) and a FREE user.
        atok, aid = await _register_login(c, ADMIN_EMAIL)
        await _promote(ADMIN_EMAIL)
        atok, aid = await _register_login(c, ADMIN_EMAIL)  # re-login to get an ADMIN-role token
        utok, uid = await _register_login(c, USER_EMAIL)
        ah = {"Authorization": f"Bearer {atok}"}
        uh = {"Authorization": f"Bearer {utok}"}

        # The FREE user must NOT reach any admin route.
        r = await c.get("/api/admin/overview", headers=uh)
        assert r.status_code == 403, r.text
        print("rbac: FREE -> 403 OK")

        # Give the user a (mock-validated) exchange key so detail has something to show.
        r = await c.post(
            "/api/exchange/connect",
            headers=uh,
            json={"exchange": "binance", "api_key": "GOODKEYabcd1234", "api_secret": "topsecret"},
        )
        assert r.status_code == 201, r.text

        # Overview rollup.
        r = await c.get("/api/admin/overview", headers=ah)
        assert r.status_code == 200, r.text
        ov = r.json()
        print(
            "overview:",
            {k: ov[k] for k in ("users", "exchange_accounts", "global_kill", "live_gate_open")},
        )
        assert ov["users"]["total"] >= 2
        assert ov["live_gate_open"] is False
        assert "by_role" in ov["users"]

        # User list (paginated) includes our user with a connected-exchange count.
        r = await c.get("/api/admin/users?limit=500", headers=ah)
        assert r.status_code == 200, r.text
        listing = r.json()
        ours = [u for u in listing["users"] if u["id"] == uid]
        assert ours and ours[0]["connected_exchanges"] == 1, ours
        print("users: listed, connected_exchanges=1 OK")

        # User detail — credentials must be last4-only, never plaintext/ciphertext.
        r = await c.get(f"/api/admin/users/{uid}", headers=ah)
        assert r.status_code == 200, r.text
        detail = r.json()
        acct = detail["exchange_accounts"][0]
        assert set(acct.keys()) == {
            "exchange",
            "label",
            "status",
            "api_key_last4",
            "can_trade",
            "can_futures",
            "can_withdraw",
        }, acct.keys()
        assert "api_secret" not in str(detail) and "encrypted" not in str(detail)
        print("detail: no secrets exposed, last4 =", acct["api_key_last4"])

        # Audit feed reachable.
        r = await c.get("/api/admin/audit?limit=10", headers=ah)
        assert r.status_code == 200 and isinstance(r.json(), list), r.text

        # Moderation: suspend the FREE user, then a suspended user cannot log in.
        r = await c.put(f"/api/admin/users/{uid}/status", headers=ah, json={"status": "SUSPENDED"})
        assert r.status_code == 200 and r.json()["status"] == "SUSPENDED", r.text
        r = await c.post("/api/auth/login", json={"email": USER_EMAIL, "password": "Sup3rSecret!"})
        assert r.status_code == 403, f"suspended user should not log in: {r.status_code}"
        print("moderation: suspend -> login 403 OK")

        # Re-activate.
        r = await c.put(f"/api/admin/users/{uid}/status", headers=ah, json={"status": "ACTIVE"})
        assert r.status_code == 200 and r.json()["status"] == "ACTIVE", r.text

        # Self-suspend guard.
        r = await c.put(f"/api/admin/users/{aid}/status", headers=ah, json={"status": "SUSPENDED"})
        assert r.status_code == 400, r.text
        print("guard: admin self-suspend -> 400 OK")

    await _purge(ADMIN_EMAIL, USER_EMAIL)
    print("ADMIN DASHBOARD E2E OK")


if __name__ == "__main__":
    asyncio.run(main())
