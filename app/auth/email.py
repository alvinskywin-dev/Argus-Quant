"""
Sprint 20A — transactional email (verification + password reset).

If SMTP is not configured (SMTP_HOST blank), the message is logged instead
of sent. This keeps local/dev deployments fully functional without an SMTP
server and guarantees we never crash on a missing mail backend.
"""

from __future__ import annotations

from email.message import EmailMessage

from app.config import settings
from app.utils.logger import logger


async def _send(to: str, subject: str, body: str) -> None:
    if not settings.smtp_configured:
        logger.info(f"[auth-email:LOGGED] to={to} subject={subject!r}\n{body}")
        return

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        import aiosmtplib

        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=settings.smtp_tls,
        )
        logger.info(f"[auth-email:SENT] to={to} subject={subject!r}")
    except Exception as exc:  # noqa: BLE001
        # Mail must never break the auth flow — log and move on.
        logger.warning(f"[auth-email:FAILED] to={to} subject={subject!r}: {exc}")


async def send_verification_email(to: str, token: str) -> None:
    link = f"{settings.app_base_url.rstrip('/')}/api/auth/verify-email?token={token}"
    body = (
        "Welcome to Argus Quant.\n\n"
        "Please verify your email address by visiting:\n"
        f"{link}\n\n"
        "If you did not create this account, you can ignore this message."
    )
    await _send(to, "Verify your Argus Quant account", body)


async def send_password_reset_email(to: str, token: str) -> None:
    link = f"{settings.app_base_url.rstrip('/')}/reset-password?token={token}"
    body = (
        "A password reset was requested for your Argus Quant account.\n\n"
        "Reset your password here:\n"
        f"{link}\n\n"
        "This link expires in 1 hour. If you did not request this, ignore it."
    )
    await _send(to, "Reset your Argus Quant password", body)
