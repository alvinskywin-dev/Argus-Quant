"""
V12 — SaaS portal shell.

Serves a single-page vanilla-JS app at /app that consumes the existing JSON
APIs (auth, paper, exchange, auto, safety, live, admin). No backend logic is
touched here; this only delivers the static shell + design-system assets from
app/dashboard/static/saas/. Pages degrade gracefully when a feature flag is off
(the API returns 404 and the UI shows a "module disabled" state).

Always mounted (the shell is static); the data behind it stays flag-gated.
Call setup_saas_app(app) from create_app().
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

_SHELL = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5"/>
<meta name="theme-color" content="#070b12"/>
<title>Argus Quant — Portal</title>
<link rel="preconnect" href="https://cdn.jsdelivr.net"/>
<link rel="stylesheet" href="/static/saas/saas.css?v=14"/>
<style>#boot{min-height:100vh;display:grid;place-items:center;color:#8fa8c7;font-family:Inter,Arial,sans-serif}</style>
</head>
<body>
<div id="boot">Loading Argus Quant…</div>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" defer></script>
<script src="/static/saas/saas.js?v=14" defer></script>
</body>
</html>
"""


def setup_saas_app(app: FastAPI) -> None:
    """Idempotently mount the /app SaaS portal shell."""
    if getattr(app.state, "_saas_app_installed", False):
        return

    @app.get("/app", response_class=HTMLResponse)
    async def saas_portal():
        return HTMLResponse(_SHELL)

    app.state._saas_app_installed = True
