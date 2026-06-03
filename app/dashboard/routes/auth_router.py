"""auth router — extracted from server.py (Phase 4).

Handlers moved verbatim; shared helpers/views/templates are imported
from app.dashboard.server. Wired via create_app().include_router().
"""

from __future__ import annotations

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.dashboard.server import (
    _admin_auth_configured,
    _admin_password,
    _admin_user,
    _login_page,
)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_get():
    return _login_page()


@router.post("/login")
async def login_post(username: str = Form(...), password: str = Form(...)):
    if not _admin_auth_configured():
        return _login_page(
            "Admin login is disabled until DASHBOARD_USER and DASHBOARD_PASSWORD are set in .env"
        )
    if username == _admin_user() and password == _admin_password():
        resp = RedirectResponse("/admin", status_code=302)
        resp.set_cookie("alpha_radar_auth", "ok", httponly=True, max_age=86400, samesite="lax")
        return resp
    return _login_page("Invalid username or password")


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("alpha_radar_auth")
    return resp
