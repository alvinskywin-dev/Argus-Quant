"""
Periodic reconciliation sweep (live-safety #3).

Runs the read-only reconciliation engine on an interval so DB↔exchange drift
(orphan positions, size/side mismatches, unprotected positions) is detected
*while trading*, not just at boot. On newly-found drift it raises an admin
alert. It NEVER opens, closes, or cancels an order — remediation stays a
separate, explicit action (recovery / emergency_close).

Gated by RECONCILIATION_LOOP_ENABLED (off by default). The pure
``summarize_critical`` helper is unit-tested without a DB or network.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from app.reconciliation.engine import SEV_CRITICAL, SEV_WARNING
from app.utils.logger import logger

AlertFn = Callable[[str, str], Awaitable[None]]

_MIN_INTERVAL_SEC = 30


def summarize_critical(result: dict) -> dict:
    """Extract critical/warning drift from a ``reconcile_all_active_users`` result.

    Returns ``{"critical": int, "warning": int, "lines": [str, ...]}``. Pure —
    no DB/network — so the alerting decision is fully testable.
    """
    critical = 0
    warning = 0
    lines: list[str] = []
    for per_user in result.get("per_user", []):
        uid = per_user.get("user_id")
        for ex_result in per_user.get("results", []):
            exchange = ex_result.get("exchange", "")
            for issue in ex_result.get("issues", []):
                sev = issue.get("severity")
                if sev == SEV_CRITICAL:
                    critical += 1
                elif sev == SEV_WARNING:
                    warning += 1
                if sev in (SEV_CRITICAL, SEV_WARNING):
                    lines.append(
                        f"[{sev}] u{uid} {exchange} {issue.get('symbol')}: "
                        f"{issue.get('issue_type')}"
                    )
    return {"critical": critical, "warning": warning, "lines": lines}


async def run_reconciliation_sweep(alert: Optional[AlertFn] = None) -> dict:
    """Run one reconciliation sweep across all active users. Read-only + audited.

    Alerts (when ``alert`` is provided) only when *new* drift was persisted this
    cycle — the engine de-dupes unresolved issues, so a standing issue is not
    re-alerted every interval.
    """
    from app.database.session import get_session
    from app.reconciliation.engine import reconcile_all_active_users

    async with get_session() as db:
        result = await reconcile_all_active_users(db, persist=True)

    summary = summarize_critical(result)
    created = int(result.get("issues_created", 0) or 0)

    if summary["critical"] or summary["warning"]:
        logger.warning(
            f"[reconcile] sweep: {summary['critical']} critical / "
            f"{summary['warning']} warning across {result.get('users', 0)} user(s); "
            f"{created} new"
        )
    else:
        logger.info(f"[reconcile] sweep clean across {result.get('users', 0)} user(s)")

    # Alert only on newly-persisted drift to avoid per-interval spam.
    from app.config import settings

    if (
        alert is not None
        and settings.reconciliation_alert_critical
        and created > 0
        and (summary["critical"] or summary["warning"])
    ):
        body = "\n".join(summary["lines"][:20]) or "see reconciliation issues"
        try:
            await alert(
                f"Reconciliation drift: {summary['critical']} critical / "
                f"{summary['warning']} warning",
                body,
            )
        except Exception as exc:  # noqa: BLE001 — alerting must never break the loop
            logger.warning(f"[reconcile] admin alert failed: {exc}")

    return result


async def reconciliation_loop(alert: Optional[AlertFn] = None) -> None:
    """Forever-loop that runs the sweep every RECONCILIATION_INTERVAL_SEC seconds.

    No-op (returns immediately) unless RECONCILIATION_LOOP_ENABLED is true, so it
    is safe to schedule unconditionally.
    """
    from app.config import settings

    if not settings.reconciliation_loop_enabled:
        logger.info("[reconcile] periodic loop disabled (RECONCILIATION_LOOP_ENABLED=false)")
        return

    interval = max(_MIN_INTERVAL_SEC, int(settings.reconciliation_interval_sec))
    logger.info(f"[reconcile] periodic loop started (every {interval}s)")
    while True:
        try:
            await run_reconciliation_sweep(alert=alert)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a sweep failure must not kill the loop
            logger.exception(f"[reconcile] sweep error (continuing): {exc}")
        await asyncio.sleep(interval)
