"""
Sprint 20C — exchange API credential vault.

Feature-flagged: inactive unless EXCHANGE_API_VAULT_ENABLED=true. Endpoints
require an authenticated user (Sprint 20A). Call setup_exchange_vault(app)
from create_app().
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_exchange_vault(app: FastAPI) -> None:
    """Idempotently mount the vault router + error handlers onto `app`."""
    if getattr(app.state, "_vault_installed", False):
        return

    from app.auth.service import AuthError
    from app.exchange_vault.router import router as vault_router
    from app.exchange_vault.service import VaultError

    async def _vault_error_handler(_request: Request, exc: VaultError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(VaultError, _vault_error_handler)  # type: ignore[arg-type]  # FastAPI: handler typed for its specific exc subtype
    app.add_exception_handler(AuthError, _auth_error_handler)  # type: ignore[arg-type]  # FastAPI: handler typed for its specific exc subtype
    app.include_router(vault_router)
    app.state._vault_installed = True
