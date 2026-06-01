"""
Sprint 21C — position recovery engine.

Feature-flagged behind POSITION_RECOVERY_ENABLED. Rebuilds local trading state
after a restart/crash and re-secures TP/SL. Opens nothing; only ever places
protective reduce-only orders. Call setup_recovery(app) from create_app() and
run_startup_recovery() once during boot.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def setup_recovery(app: FastAPI) -> None:
    if getattr(app.state, "_recovery_installed", False):
        return
    from app.auth.service import AuthError
    from app.recovery.router import router as recovery_router

    async def _auth_error_handler(_request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.add_exception_handler(AuthError, _auth_error_handler)
    app.include_router(recovery_router)
    app.state._recovery_installed = True


async def run_startup_recovery() -> dict:
    """
    One-shot recovery sweep at boot. Safe to call unconditionally — it no-ops
    unless POSITION_RECOVERY_ENABLED is true, and never raises.
    """
    from app.config import settings
    from app.utils.logger import logger
    if not settings.position_recovery_enabled:
        return {"skipped": "POSITION_RECOVERY_ENABLED is false"}
    try:
        from app.database.session import get_session
        from app.recovery import engine
        async with get_session() as db:
            result = await engine.recover_all_positions(db)
        logger.info(f"[recovery] startup sweep: {result.get('users')} users, "
                    f"{result.get('recovered')} recovered, {result.get('unsafe')} unsafe")
        return result
    except Exception as exc:  # noqa: BLE001 — recovery must never block startup
        logger.warning(f"[recovery] startup sweep failed (non-fatal): {exc!s:.160}")
        return {"error": str(exc)[:160]}
